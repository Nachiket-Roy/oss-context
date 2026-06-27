"""Command-line interface for oss-context.

This module defines Typer commands for syncing GitHub data, querying the local
SQLite knowledge graph for PR and issue context, serving the MCP endpoint, and
launching the local HTML UI.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console

from oss_context.db import DatabaseManager
from oss_context.formatting import (
    render_dashboard,
    render_decisions,
    render_issue_context,
    render_pr_context,
    render_pr_health,
    render_reviewer_status,
    render_sync_report,
    render_tracked_repos,
    render_unresolved_threads,
)
from oss_context.mcp_server import run_mcp_server
from oss_context.models import RepoRef
from oss_context.queries import (
    get_dashboard_summary,
    get_issue_context_payload,
    get_pr_context_payload,
    get_pr_decisions,
    get_pr_health,
    get_reviewer_status,
    list_tracked_repos,
    list_unresolved_threads,
)
from oss_context.settings import load_settings
from oss_context.sync import sync_repository
from oss_context.web_ui import serve_web_ui

app = typer.Typer(help="Track GitHub PR and issue context in a local SQLite knowledge graph.")
console = Console()


def _normalize_repo(repo: str | None) -> str | None:
    """Normalize and validate owner/name repository slugs."""
    if repo is None:
        return None
    try:
        return RepoRef.from_slug(repo).slug
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--repo") from exc


def _load_cli_settings(db_path: Path | None):
    """Load validated runtime settings for CLI commands."""
    try:
        return load_settings(db_path)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command()
def sync(
    repo: str,
    db_path: Path | None = typer.Option(None, help="Override the SQLite database path."),
    extract_decisions: bool = typer.Option(
        True, help="Run Phase 1 decision extraction after sync."
    ),
    batch_size: int = typer.Option(10, min=1, help="Comments to analyze per LLM batch."),
) -> None:
    """Sync a GitHub repository into the local database."""
    settings = _load_cli_settings(db_path)
    normalized_repo = _normalize_repo(repo)
    if normalized_repo is None:
        raise typer.BadParameter("--repo is required", param_hint="repo")
    report = asyncio.run(
        sync_repository(
            normalized_repo,
            settings,
            extract_decisions=extract_decisions,
            batch_size=batch_size,
        )
    )
    console.print(render_sync_report(report))


@app.command()
def query(
    repo: str | None = typer.Option(None, help="Filter by repository in owner/name form."),
    pr: int | None = typer.Option(None, help="Pull request number."),
    issue: int | None = typer.Option(None, help="Issue number."),
    unresolved: bool = typer.Option(False, help="Show unresolved threads."),
    decisions: bool = typer.Option(False, help="Show extracted decisions for a PR."),
    health: bool = typer.Option(False, help="Show health summary for a PR."),
    context: bool = typer.Option(
        False,
        help="Show full PR or issue context, including references.",
    ),
    dashboard: bool = typer.Option(False, help="Show a cross-repo dashboard summary."),
    repos: bool = typer.Option(False, help="Show tracked repository sync status."),
    reviewer_status: bool = typer.Option(
        False,
        help="Show reviewer status instead of using --author only as a filter.",
    ),
    author: str | None = typer.Option(
        None,
        "--author",
        "--reviewer",
        help="Filter unresolved threads by reviewer.",
    ),
    label: str | None = typer.Option(None, help="Filter unresolved threads by PR label."),
    stale: int | None = typer.Option(
        None,
        help="Show only unresolved threads stale for at least N days.",
    ),
    pending: bool = typer.Option(
        False,
        help="Show only unresolved threads waiting on the reviewer to respond.",
    ),
    db_path: Path | None = typer.Option(None, help="Override the SQLite database path."),
) -> None:
    """Query unresolved state, PR health, issue context, and repo dashboards."""
    normalized_repo = _normalize_repo(repo)
    if pr is not None and issue is not None:
        raise typer.BadParameter("--pr and --issue cannot be used together")
    if (pr is not None or issue is not None) and normalized_repo is None:
        raise typer.BadParameter("--repo is required when using --pr or --issue")
    if issue is not None and (unresolved or decisions or health):
        raise typer.BadParameter(
            "--issue cannot be combined with --unresolved, --decisions, or --health"
        )
    if decisions and pr is None:
        raise typer.BadParameter("--pr is required with --decisions")
    if health and pr is None:
        raise typer.BadParameter("--pr is required with --health")
    if reviewer_status and not author:
        raise typer.BadParameter("--author/--reviewer is required with --reviewer-status")
    if pr is not None and not (decisions or health or context):
        raise typer.BadParameter("--pr requires --decisions, --health, or --context")

    settings = _load_cli_settings(db_path)
    connection = DatabaseManager(settings.db_path).initialize()
    try:
        if repos:
            console.print(
                render_tracked_repos(list_tracked_repos(connection, repo=normalized_repo))
            )

        if dashboard:
            summary = get_dashboard_summary(
                connection,
                repo=normalized_repo,
                reviewer=author,
                label=label,
                stale_days=stale if stale is not None else 7,
            )
            console.print(render_dashboard(summary))

        if decisions:
            assert normalized_repo is not None
            assert pr is not None
            rows = get_pr_decisions(connection, repo=normalized_repo, pr_number=pr)
            console.print(render_decisions(rows, repo=normalized_repo, pr_number=pr))

        if health:
            assert normalized_repo is not None
            assert pr is not None
            summary = get_pr_health(connection, repo=normalized_repo, pr_number=pr)
            console.print(render_pr_health(summary))

        if reviewer_status:
            assert author is not None
            status = get_reviewer_status(connection, repo=normalized_repo, reviewer=author)
            console.print(render_reviewer_status(status))

        if context:
            if pr is not None:
                assert normalized_repo is not None
                payload = get_pr_context_payload(connection, repo=normalized_repo, pr_number=pr)
                console.print(render_pr_context(payload))
            elif issue is not None:
                assert normalized_repo is not None
                payload = get_issue_context_payload(
                    connection,
                    repo=normalized_repo,
                    issue_number=issue,
                )
                console.print(render_issue_context(payload))
            else:
                raise typer.BadParameter("--context requires --pr or --issue")

        explicit_view_selected = any(
            [
                unresolved,
                decisions,
                health,
                context,
                dashboard,
                repos,
                reviewer_status,
            ]
        )
        if not explicit_view_selected:
            if issue is not None:
                assert normalized_repo is not None
                payload = get_issue_context_payload(
                    connection,
                    repo=normalized_repo,
                    issue_number=issue,
                )
                console.print(render_issue_context(payload))
            else:
                rows = list_unresolved_threads(
                    connection,
                    repo=normalized_repo,
                    author=author,
                    label=label,
                    stale_days=stale,
                    pending_only=pending,
                )
                console.print(render_unresolved_threads(rows))
        elif unresolved:
            rows = list_unresolved_threads(
                connection,
                repo=normalized_repo,
                author=author,
                label=label,
                stale_days=stale,
                pending_only=pending,
            )
            console.print(render_unresolved_threads(rows))
    finally:
        connection.close()


@app.command()
def serve(
    transport: str = typer.Option(
        "stdio",
        help="MCP transport to use: stdio or http.",
    ),
    host: str = typer.Option("127.0.0.1", help="HTTP bind host when using --transport http."),
    port: int = typer.Option(8765, min=1, help="HTTP port when using --transport http."),
    db_path: Path | None = typer.Option(None, help="Override the SQLite database path."),
) -> None:
    """Run the MCP server for IDE and agent integration."""
    normalized_transport = transport.strip().lower()
    if normalized_transport not in {"stdio", "http"}:
        raise typer.BadParameter("--transport must be either 'stdio' or 'http'")
    settings = _load_cli_settings(db_path)
    run_mcp_server(settings, transport=normalized_transport, host=host, port=port)


@app.command()
def ui(
    host: str = typer.Option("127.0.0.1", help="Bind host for the local HTML UI."),
    port: int = typer.Option(8080, min=1, help="Port for the local HTML UI."),
    db_path: Path | None = typer.Option(None, help="Override the SQLite database path."),
) -> None:
    """Serve the local HTML dashboard and PR/issue detail pages."""
    settings = _load_cli_settings(db_path)
    serve_web_ui(settings, host=host, port=port)


if __name__ == "__main__":
    app()
