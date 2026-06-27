"""Command-line interface for oss-context.

This module defines the Typer commands used to sync GitHub pull-request data,
query the local SQLite knowledge graph, inspect cross-repo review load, and
run the Phase 2 MCP server for IDE and agent integrations.
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
    get_pr_decisions,
    get_pr_health,
    get_reviewer_status,
    list_tracked_repos,
    list_unresolved_threads,
)
from oss_context.settings import load_settings
from oss_context.sync import sync_repository

app = typer.Typer(help="Track GitHub PR decision state in a local SQLite knowledge graph.")
console = Console()


def _normalize_repo(repo: str | None) -> str | None:
    if repo is None:
        return None
    try:
        return RepoRef.from_slug(repo).slug
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--repo") from exc


def _load_cli_settings(db_path: Path | None):
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
    unresolved: bool = typer.Option(False, help="Show unresolved threads."),
    decisions: bool = typer.Option(False, help="Show extracted decisions for a PR."),
    health: bool = typer.Option(False, help="Show health summary for a PR."),
    dashboard: bool = typer.Option(False, help="Show a cross-repo dashboard summary."),
    repos: bool = typer.Option(False, help="Show tracked repository sync status."),
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
    all_repos: bool = typer.Option(
        False,
        help="Explicitly query across all tracked repositories.",
    ),
    db_path: Path | None = typer.Option(None, help="Override the SQLite database path."),
) -> None:
    """Query unresolved state, extracted decisions, PR health, and repo dashboards."""
    normalized_repo = _normalize_repo(repo)
    if all_repos and normalized_repo is not None:
        raise typer.BadParameter("--all-repos cannot be used together with --repo")

    settings = _load_cli_settings(db_path)
    connection = DatabaseManager(settings.db_path).initialize()
    try:
        if pr is not None and not normalized_repo:
            raise typer.BadParameter("--repo is required when using --pr")

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
                stale_days=stale or 7,
            )
            console.print(render_dashboard(summary))

        if decisions:
            if pr is None or normalized_repo is None:
                raise typer.BadParameter("--repo and --pr are required with --decisions")
            rows = get_pr_decisions(connection, repo=normalized_repo, pr_number=pr)
            console.print(render_decisions(rows, repo=normalized_repo, pr_number=pr))

        if health:
            if pr is None or normalized_repo is None:
                raise typer.BadParameter("--repo and --pr are required with --health")
            summary = get_pr_health(connection, repo=normalized_repo, pr_number=pr)
            console.print(render_pr_health(summary))

        if author and not decisions and not health:
            reviewer_status = get_reviewer_status(connection, repo=normalized_repo, reviewer=author)
            console.print(render_reviewer_status(reviewer_status))

        if unresolved or (not decisions and not health and not dashboard and not repos):
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
    """Run the Phase 2 MCP server for IDE and agent integration."""
    normalized_transport = transport.strip().lower()
    if normalized_transport not in {"stdio", "http"}:
        raise typer.BadParameter("--transport must be either 'stdio' or 'http'")
    settings = _load_cli_settings(db_path)
    run_mcp_server(settings, transport=normalized_transport, host=host, port=port)


if __name__ == "__main__":
    app()
