"""MCP server integration for oss-context.

This module exposes the local SQLite knowledge graph through FastMCP tools and
resources so IDEs and agents can query PR context, issue context, reviewer
state, unresolved threads, dashboard summaries, and on-demand sync operations.
"""

from __future__ import annotations

from pathlib import Path

from fastmcp import FastMCP

from oss_context import architecture
from oss_context.code_index import (
    get_combined_file_context,
    get_impacted_files,
    get_symbol_callees,
    get_symbol_callers,
    index_codebase,
    search_symbols,
)
from oss_context.db import DatabaseManager
from oss_context.markdown import (
    render_dashboard_markdown,
    render_file_context_markdown,
    render_issue_context_markdown,
    render_merge_readiness_markdown,
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
from oss_context.review_assistant import get_merge_readiness_payload
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

    @mcp.tool()
    def index_code(
        cwd: str | None = None,
        repo: str | None = None,
    ) -> dict:
        """Index Python files from a local workspace into the SQLite code graph."""
        normalized_repo = _normalize_repo(repo)
        connection = _connect()
        try:
            return index_codebase(
                connection,
                cwd=Path(cwd) if cwd else None,
                repo=normalized_repo,
            )
        finally:
            connection.close()

    @mcp.tool()
    def search_code(
        query: str,
        repo: str | None = None,
        branch: str | None = None,
        limit: int = 25,
    ) -> list[dict]:
        """Search indexed symbols from the latest code snapshot scope."""
        normalized_repo = _normalize_repo(repo)
        connection = _connect()
        try:
            return search_symbols(
                connection,
                query=query,
                repo=normalized_repo,
                branch=branch,
                limit=limit,
            )
        finally:
            connection.close()

    @mcp.tool()
    def get_symbol_callers_tool(
        symbol: str,
        repo: str | None = None,
        branch: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """List indexed callers for a symbol."""
        normalized_repo = _normalize_repo(repo)
        connection = _connect()
        try:
            return get_symbol_callers(
                connection,
                symbol=symbol,
                repo=normalized_repo,
                branch=branch,
                limit=limit,
            )
        finally:
            connection.close()

    @mcp.tool()
    def get_symbol_callees_tool(
        symbol: str,
        repo: str | None = None,
        branch: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """List indexed outgoing calls made by a symbol."""
        normalized_repo = _normalize_repo(repo)
        connection = _connect()
        try:
            return get_symbol_callees(
                connection,
                symbol=symbol,
                repo=normalized_repo,
                branch=branch,
                limit=limit,
            )
        finally:
            connection.close()

    @mcp.tool()
    def get_impacted_files_tool(
        symbol: str,
        repo: str | None = None,
        branch: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """List files directly impacted by a symbol and its callers."""
        normalized_repo = _normalize_repo(repo)
        connection = _connect()
        try:
            return get_impacted_files(
                connection,
                symbol=symbol,
                repo=normalized_repo,
                branch=branch,
                limit=limit,
            )
        finally:
            connection.close()

    @mcp.tool()
    def get_file_context(
        file_path: str,
        repo: str | None = None,
        branch: str | None = None,
        cwd: str | None = None,
    ) -> str:
        """Get combined code and review context for a file."""
        normalized_repo = _normalize_repo(repo)
        connection = _connect()
        try:
            payload = get_combined_file_context(
                connection,
                file_path=file_path,
                repo=normalized_repo,
                branch=branch,
                cwd=Path(cwd) if cwd else None,
            )
        finally:
            connection.close()
        return render_file_context_markdown(payload)

    @mcp.tool()
    def get_merge_readiness(repo: str, pr_number: int, stale_days: int = 3) -> str:
        """Summarize what remains before a PR is likely ready to merge."""
        normalized_repo = RepoRef.from_slug(repo).slug
        connection = _connect()
        try:
            payload = get_merge_readiness_payload(
                connection,
                repo=normalized_repo,
                pr_number=pr_number,
                stale_days=stale_days,
            )
        finally:
            connection.close()
        return render_merge_readiness_markdown(payload)

    @mcp.tool()
    async def explain_implementation(repo: str, file_path: str) -> str:
        """Explain why a file looks the way it does using architectural memory."""
        connection = DatabaseManager(settings.db_path).initialize()
        try:
            repo_row = connection.execute(
                "SELECT id FROM repos WHERE owner = ? AND name = ?",
                tuple(RepoRef.from_slug(repo).slug.split("/"))
            ).fetchone()
            if not repo_row:
                return f"Repo {repo} not found."
            return await architecture.explain_code(connection, repo_row["id"], repo, file_path)
        finally:
            connection.close()

    @mcp.tool()
    async def get_design_summary(repo: str, target_type: str, target_id: int) -> str:
        """Get the architectural design memory for a PR or issue."""
        connection = DatabaseManager(settings.db_path).initialize()
        try:
            repo_row = connection.execute(
                "SELECT id FROM repos WHERE owner = ? AND name = ?",
                tuple(RepoRef.from_slug(repo).slug.split("/"))
            ).fetchone()
            if not repo_row:
                return f"Repo {repo} not found."
            memory = await architecture.generate_architectural_memory(connection, target_type, target_id, repo_row["id"])  # noqa: E501
            import json
            return json.dumps(memory, indent=2)
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

    @mcp.resource("pr://{owner}/{name}/{pr_number}/design")
    def pr_design(owner: str, name: str, pr_number: int) -> str:
        """Fetch architectural design memory for a specific pull request."""
        connection = DatabaseManager(settings.db_path).initialize()
        try:
            repo_row = connection.execute("SELECT id FROM repos WHERE owner = ? AND name = ?", (owner, name)).fetchone()  # noqa: E501
            if not repo_row:
                return ""
            import asyncio
            memory = asyncio.run(architecture.generate_architectural_memory(connection, "pr", pr_number, repo_row["id"]))  # noqa: E501
            import json
            return json.dumps(memory, indent=2)
        finally:
            connection.close()

    @mcp.resource("issue://{owner}/{name}/{issue_number}/design")
    def issue_design(owner: str, name: str, issue_number: int) -> str:
        """Fetch architectural design memory for a specific issue."""
        connection = DatabaseManager(settings.db_path).initialize()
        try:
            repo_row = connection.execute("SELECT id FROM repos WHERE owner = ? AND name = ?", (owner, name)).fetchone()  # noqa: E501
            if not repo_row:
                return ""
            import asyncio
            memory = asyncio.run(architecture.generate_architectural_memory(connection, "issue", issue_number, repo_row["id"]))  # noqa: E501
            import json
            return json.dumps(memory, indent=2)
        finally:
            connection.close()

    @mcp.resource("code://{owner}/{name}/why/{path}")
    def code_why_resource(owner: str, name: str, path: str) -> str:
        """Explain why a file looks the way it does using architectural memory."""
        connection = DatabaseManager(settings.db_path).initialize()
        try:
            repo_row = connection.execute("SELECT id FROM repos WHERE owner = ? AND name = ?", (owner, name)).fetchone()  # noqa: E501
            if not repo_row:
                return ""
            import asyncio
            return asyncio.run(architecture.explain_code(connection, repo_row["id"], f"{owner}/{name}", path))  # noqa: E501
        finally:
            connection.close()

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

    @mcp.resource("pr://{owner}/{name}/{pr_number}/merge-readiness")
    def merge_readiness_resource(owner: str, name: str, pr_number: int) -> str:
        """Read markdown merge-readiness guidance for a PR."""
        return get_merge_readiness(f"{owner}/{name}", pr_number)

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
