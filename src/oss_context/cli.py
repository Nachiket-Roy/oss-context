from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console

from oss_context.db import DatabaseManager
from oss_context.formatting import (
    render_decisions,
    render_pr_health,
    render_sync_report,
    render_unresolved_threads,
)
from oss_context.models import RepoRef
from oss_context.queries import get_pr_decisions, get_pr_health, list_unresolved_threads
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
    author: str | None = typer.Option(None, help="Filter unresolved threads by reviewer."),
    label: str | None = typer.Option(None, help="Filter unresolved threads by PR label."),
    stale: int | None = typer.Option(
        None, help="Show only unresolved threads stale for at least N days."
    ),
    db_path: Path | None = typer.Option(None, help="Override the SQLite database path."),
) -> None:
    """Query unresolved state, extracted decisions, and PR health."""
    normalized_repo = _normalize_repo(repo)
    settings = _load_cli_settings(db_path)
    connection = DatabaseManager(settings.db_path).initialize()
    try:
        if pr is not None and not normalized_repo:
            raise typer.BadParameter("--repo is required when using --pr")

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

        if unresolved or (not decisions and not health):
            rows = list_unresolved_threads(
                connection,
                repo=normalized_repo,
                author=author,
                label=label,
                stale_days=stale,
            )
            console.print(render_unresolved_threads(rows))
    finally:
        connection.close()


@app.command()
def serve() -> None:
    """Reserved for the Phase 2 MCP server."""
    console.print(
        "Phase 2 will add the MCP server. For now, use `oss-context sync` and `oss-context query`."
    )


if __name__ == "__main__":
    app()
