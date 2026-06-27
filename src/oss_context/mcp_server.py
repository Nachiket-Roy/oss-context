"""MCP server integration for oss-context.

This module exposes the local SQLite knowledge graph through FastMCP tools and
resources so IDEs and agents can query PR context, issue context, reviewer
state, unresolved threads, dashboard summaries, and on-demand sync operations.
"""

from __future__ import annotations

from fastmcp import FastMCP

from oss_context.db import DatabaseManager
from oss_context.markdown import (
    render_dashboard_markdown,
    render_issue_context_markdown,
    render_pr_context_markdown,
    render_repo_sync_markdown,
    render_reviewer_status_markdown,
    render_unresolved_threads_markdown,
)
from oss_context.models import RepoRef
from oss_context.queries import (
    get_dashboard_summary,
    get_issue_context_payload,
    get_pr_context_payload,
    get_repo_sync_status,
    get_reviewer_status,
    list_unresolved_threads,
    search_work_items,
)
from oss_context.settings import Settings
from oss_context.sync import sync_repository


def create_mcp_server(settings: Settings) -> FastMCP:
    """Create the FastMCP server exposing oss-context tools and resources."""
    mcp = FastMCP(
        "oss-context",
        instructions=(
            "Provides pull-request and issue context from a local SQLite knowledge graph. "
            "Start with dashboard or unresolved-thread queries before asking for a "
            "specific PR or issue context."
        ),
    )

    def _connect():
        return DatabaseManager(settings.db_path).initialize()

    def _normalize_repo(repo: str | None) -> str | None:
        if repo is None:
            return None
        return RepoRef.from_slug(repo).slug

    @mcp.tool()
    async def sync_repo(
        repo: str,
        extract_decisions: bool = True,
        batch_size: int = 10,
    ) -> dict:
        """Sync a repository and optionally run decision extraction."""
        normalized_repo = RepoRef.from_slug(repo).slug
        report = await sync_repository(
            normalized_repo,
            settings,
            extract_decisions=extract_decisions,
            batch_size=batch_size,
        )
        return report.model_dump(mode="json")

    @mcp.tool()
    def get_pr_context(repo: str, pr_number: int) -> str:
        """Get markdown context for a PR, including health, decisions, threads, and links."""
        normalized_repo = RepoRef.from_slug(repo).slug
        connection = _connect()
        try:
            payload = get_pr_context_payload(connection, repo=normalized_repo, pr_number=pr_number)
        finally:
            connection.close()
        return render_pr_context_markdown(payload)

    @mcp.tool()
    def get_issue_context(repo: str, issue_number: int) -> str:
        """Get markdown context for an issue, including references and backreferences."""
        normalized_repo = RepoRef.from_slug(repo).slug
        connection = _connect()
        try:
            payload = get_issue_context_payload(
                connection,
                repo=normalized_repo,
                issue_number=issue_number,
            )
        finally:
            connection.close()
        return render_issue_context_markdown(payload)

    @mcp.tool()
    def get_unresolved_threads(
        repo: str | None = None,
        reviewer: str | None = None,
        label: str | None = None,
        stale_days: int | None = None,
        pending_only: bool = False,
    ) -> list[dict]:
        """List unresolved review threads across one repo or all synced repos."""
        normalized_repo = _normalize_repo(repo)
        connection = _connect()
        try:
            return list_unresolved_threads(
                connection,
                repo=normalized_repo,
                author=reviewer,
                label=label,
                stale_days=stale_days,
                pending_only=pending_only,
            )
        finally:
            connection.close()

    @mcp.tool()
    def get_reviewer_state(reviewer: str, repo: str | None = None) -> dict:
        """Summarize what a reviewer is blocking and what still needs their response."""
        normalized_repo = _normalize_repo(repo)
        connection = _connect()
        try:
            return get_reviewer_status(connection, repo=normalized_repo, reviewer=reviewer)
        finally:
            connection.close()

    @mcp.tool()
    def get_dashboard(
        repo: str | None = None,
        reviewer: str | None = None,
        label: str | None = None,
        stale_days: int = 7,
    ) -> dict:
        """Get a dashboard summary with repo and reviewer breakdowns."""
        normalized_repo = _normalize_repo(repo)
        connection = _connect()
        try:
            return get_dashboard_summary(
                connection,
                repo=normalized_repo,
                reviewer=reviewer,
                label=label,
                stale_days=stale_days,
            )
        finally:
            connection.close()

    @mcp.tool()
    def search_work(
        text: str | None = None,
        reference: str | None = None,
        repo: str | None = None,
        state: str | None = None,
        limit: int = 25,
    ) -> dict:
        """Search synced pull requests and issues by text and/or structured references."""
        normalized_repo = _normalize_repo(repo)
        connection = _connect()
        try:
            return search_work_items(
                connection,
                repo=normalized_repo,
                text=text,
                reference=reference,
                state=state,
                limit=limit,
            )
        finally:
            connection.close()

    @mcp.resource("pr://{owner}/{name}/{pr_number}/context")
    def pr_context_resource(owner: str, name: str, pr_number: int) -> str:
        """Read markdown PR context using a resource URI."""
        return get_pr_context(f"{owner}/{name}", pr_number)

    @mcp.resource("issue://{owner}/{name}/{issue_number}/context")
    def issue_context_resource(owner: str, name: str, issue_number: int) -> str:
        """Read markdown issue context using a resource URI."""
        return get_issue_context(f"{owner}/{name}", issue_number)

    @mcp.resource("pr://{owner}/{name}/unresolved")
    def unresolved_resource(owner: str, name: str) -> str:
        """Read unresolved-thread markdown for a repository."""
        connection = _connect()
        try:
            rows = list_unresolved_threads(connection, repo=f"{owner}/{name}")
        finally:
            connection.close()
        return render_unresolved_threads_markdown(
            rows,
            title=f"Unresolved threads · {owner}/{name}",
        )

    @mcp.resource("pr://{owner}/{name}/freshness")
    def freshness_resource(owner: str, name: str) -> str:
        """Read sync freshness and repo-level summary for a repository."""
        connection = _connect()
        try:
            status = get_repo_sync_status(connection, repo=f"{owner}/{name}")
        finally:
            connection.close()
        return render_repo_sync_markdown(status)

    @mcp.resource("pr://dashboard/overview")
    def dashboard_resource() -> str:
        """Read the cross-repo markdown dashboard overview."""
        connection = _connect()
        try:
            summary = get_dashboard_summary(connection)
        finally:
            connection.close()
        return render_dashboard_markdown(summary)

    @mcp.resource("pr://reviewer/{reviewer}/status")
    def reviewer_resource(reviewer: str) -> str:
        """Read markdown reviewer status across all synced repos."""
        connection = _connect()
        try:
            status = get_reviewer_status(connection, reviewer=reviewer)
        finally:
            connection.close()
        return render_reviewer_status_markdown(status)

    return mcp


def run_mcp_server(
    settings: Settings,
    *,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    """Run the oss-context MCP server using the selected transport."""
    server = create_mcp_server(settings)
    if transport == "stdio":
        server.run()
        return
    if transport == "http":
        server.run(transport="http", host=host, port=port)
        return
    raise ValueError("transport must be either 'stdio' or 'http'")
