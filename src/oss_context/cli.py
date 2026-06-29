"""Command-line interface for oss-context.

This module defines Typer commands for syncing GitHub data, querying the local
SQLite knowledge graph for PR and issue context, serving the MCP endpoint,
launching the local HTML UI, and driving branch-aware workflows.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console

from oss_context import architecture
from oss_context.branch_context import (
    BranchContextError,
    get_branch_context_payload,
    get_branch_file_context,
    get_git_repo_root,
    get_git_worktree,
    link_branch_to_pr,
    resolve_branch_pr,
)
from oss_context.code_index import (
    get_combined_file_context,
    get_impacted_files,
    get_symbol_callees,
    get_symbol_callers,
    index_codebase,
    search_symbols,
)
from oss_context.db import DatabaseManager
from oss_context.formatting import (
    render_branch_context,
    render_branch_file_context,
    render_branch_resolution,
    render_code_index_report,
    render_dashboard,
    render_decisions,
    render_file_context_report,
    render_hook_installation,
    render_impacted_files,
    render_issue_context,
    render_merge_readiness,
    render_pr_context,
    render_pr_health,
    render_retrieval_doctor,
    render_reviewer_status,
    render_symbol_search,
    render_sync_report,
    render_tracked_repos,
    render_unresolved_threads,
)
from oss_context.github import GitHubApiError
from oss_context.hooks import HookInstallError, install_git_hooks
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
from oss_context.retrieval import run_retrieval_doctor
from oss_context.review_assistant import get_merge_readiness_payload
from oss_context.settings import load_settings
from oss_context.sync import sync_repository, sync_single_issue, sync_single_pr
from oss_context.web_ui import serve_web_ui

app = typer.Typer(help="Track GitHub PR and issue context in a local SQLite knowledge graph.")
branch_app = typer.Typer(help="Resolve the current git branch to pull-request context.")
code_app = typer.Typer(help="Index local code and query symbol-aware repository intelligence.")
review_app = typer.Typer(help="Summarize merge-readiness and follow-up review actions.")
doctor_app = typer.Typer(help="Inspect retrieval quality and local data-health issues.")
app.add_typer(branch_app, name="branch")
app.add_typer(code_app, name="code")
app.add_typer(review_app, name="review")
app.add_typer(doctor_app, name="doctor")
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


def _fail_branch_command(exc: Exception, *, quiet: bool = False) -> None:
    """Exit a branch workflow command with a concise, user-facing error."""
    if not quiet:
        console.print(f"[red]{exc}[/red]")
    raise typer.Exit(code=1) from exc


@app.command()
def sync(
    repo: str,
    db_path: Path | None = typer.Option(None, help="Override the SQLite database path."),
    extract_decisions: bool = typer.Option(True, help="Run decision extraction after sync."),
    batch_size: int = typer.Option(10, min=1, help="Comments to analyze per LLM batch."),
    limit: int | None = typer.Option(
        None,
        help="Sync up to a custom number of PRs and issues.",
    ),
    all_history: bool = typer.Option(
        False,
        "--all",
        help="Sync full historical pull requests and issues.",
    ),
    since: str | None = typer.Option(
        None,
        help="Sync only work updated since duration (e.g. 90d, 24h, 1y).",
    ),
    pr: int | None = typer.Option(None, help="Targeted sync of a single pull request."),
    issue: int | None = typer.Option(None, help="Targeted sync of a single issue."),
) -> None:
    """Sync a GitHub repository into the local database.

    By default, requires explicit intent via --pr, --issue, --limit, --since, or --all.
    """
    settings = _load_cli_settings(db_path)
    normalized_repo = _normalize_repo(repo)
    if normalized_repo is None:
        raise typer.BadParameter("--repo is required", param_hint="repo")

    since_override = None
    if since:
        import re
        from datetime import UTC, datetime, timedelta
        match = re.match(r"^(\d+)([dhy])$", since.strip().lower())
        if not match:
            raise typer.BadParameter(
                "Invalid since duration format. Use e.g. 90d, 24h, 1y",
                param_hint="since",
            )
        value, unit = match.groups()
        val = int(value)
        if unit == "d":
            delta = timedelta(days=val)
        elif unit == "h":
            delta = timedelta(hours=val)
        elif unit == "y":
            delta = timedelta(days=val * 365)
        else:
            raise typer.BadParameter(
                "Unsupported unit. Use h (hours), d (days), or y (years)",
                param_hint="since",
            )
        since_override = datetime.now(UTC) - delta

    sync_limit = None if all_history else limit

    try:
        if pr is not None and issue is not None:
            console.print("[red]Cannot specify both --pr and --issue.[/red]")
            raise typer.Exit(code=1)

        if pr is not None:
            console.print(f"Targeted sync of PR #{pr}...")
            asyncio.run(sync_single_pr(normalized_repo, pr, settings))
            console.print("Done.")
            return

        if issue is not None:
            console.print(f"Targeted sync of issue #{issue}...")
            asyncio.run(sync_single_issue(normalized_repo, issue, settings))
            console.print("Done.")
            return

        if sync_limit is None and not all_history and not since_override:
            console.print(
                "[red]No sync target specified.[/red] "
                "Use --pr <num>, --issue <num>, --limit <N>, --since <duration>, or --all."
            )
            raise typer.Exit(code=1)

        report = asyncio.run(
            sync_repository(
                normalized_repo,
                settings,
                extract_decisions=extract_decisions,
                batch_size=batch_size,
                limit=sync_limit,
                since_override=since_override,
            )
        )
        console.print(render_sync_report(report))
    except GitHubApiError as exc:
        is_graphql = "graphql" in str(exc).lower()
        api_type = "GraphQL" if is_graphql else "API"
        console.print(f"GitHub {api_type} request failed.\n")
        if exc.repo:
            console.print(f"Repository: {exc.repo}")
        if exc.operation:
            console.print(f"Operation: {exc.operation}")
        if exc.http_status:
            console.print(f"HTTP status: {exc.http_status}")
        if exc.response_text:
            console.print("Response:")
            try:
                import json
                parsed = json.loads(exc.response_text)
                console.print(json.dumps(parsed, indent=2))
            except Exception:
                console.print(exc.response_text)
        else:
            console.print(f"Error: {exc}")

        console.print("\nHint:")
        console.print("- Check GITHUB_TOKEN")
        console.print("- Verify token permissions")
        console.print("- Check rate limits")
        raise typer.Exit(code=1) from None


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
    design: bool = typer.Option(False, help="Show architectural design memory."),
    rationale: bool = typer.Option(False, help="Show rationale graph."),
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
    if context and pr is None and issue is None:
        raise typer.BadParameter("--context requires --pr or --issue")
    if (design or rationale) and pr is None and issue is None:
        raise typer.BadParameter("--design and --rationale require --pr or --issue")
    if pr is not None and not (decisions or health or context or design or rationale):
        raise typer.BadParameter("--pr requires --decisions, --health, --context, --design, or --rationale")  # noqa: E501
    if issue is not None and not (context or design or rationale):
        raise typer.BadParameter("--issue requires --context, --design, or --rationale")

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
                raise AssertionError("preflight validation should require --pr or --issue")
                
        if design or rationale:
            import json
            target_type = "pr" if pr is not None else "issue"
            target_id = pr if pr is not None else issue
            assert normalized_repo is not None
            assert target_id is not None
            # get repo_id
            repo_row = connection.execute("SELECT id FROM repos WHERE owner = ? AND name = ?", tuple(normalized_repo.split("/"))).fetchone()  # noqa: E501
            if not repo_row:
                console.print(f"Repository {normalized_repo} not found in database.")
                raise typer.Exit(code=1)
            
            memory = asyncio.run(architecture.generate_architectural_memory(
                connection, target_type, target_id, repo_row["id"]
            ))
            if design:
                console.print("\n[bold cyan]Architectural Design Memory[/bold cyan]")
                console.print(json.dumps({"design_summary": memory.get("design_summary"), "decisions": memory.get("decisions"), "implementation": memory.get("implementation")}, indent=2))  # noqa: E501
            if rationale:
                console.print("\n[bold cyan]Rationale Graph Links[/bold cyan]")
                console.print(json.dumps(memory.get("rationale_links", []), indent=2))

        explicit_view_selected = any(
            [
                unresolved,
                decisions,
                health,
                context,
                design,
                rationale,
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


@code_app.command("index")
def code_index_command(
    cwd: Path | None = typer.Option(None, help="Workspace root to index."),
    repo: str | None = typer.Option(None, help="Override the detected GitHub repo slug."),
    db_path: Path | None = typer.Option(None, help="Override the SQLite database path."),
) -> None:
    """Index Python files from the local workspace into SQLite."""
    normalized_repo = _normalize_repo(repo)
    settings = _load_cli_settings(db_path)
    connection = DatabaseManager(settings.db_path).initialize()
    try:
        report = index_codebase(connection, cwd=cwd, repo=normalized_repo)
        console.print(render_code_index_report(report))
    finally:
        connection.close()


@code_app.command("search")
def code_search(
    query: str = typer.Argument(..., help="Symbol name or qualified-name fragment to search for."),
    repo: str | None = typer.Option(None, help="Filter by repository in owner/name form."),
    branch: str | None = typer.Option(None, help="Filter by indexed git branch name."),
    limit: int = typer.Option(25, min=1, help="Maximum number of results to return."),
    db_path: Path | None = typer.Option(None, help="Override the SQLite database path."),
) -> None:
    """Search indexed symbols from the latest snapshot scope."""
    normalized_repo = _normalize_repo(repo)
    settings = _load_cli_settings(db_path)
    connection = DatabaseManager(settings.db_path).initialize()
    try:
        rows = search_symbols(
            connection, query=query, repo=normalized_repo, branch=branch, limit=limit
        )
        console.print(render_symbol_search(rows, title=f"Symbol search · {query}"))
    finally:
        connection.close()


@code_app.command("callers")
def code_callers(
    symbol: str = typer.Argument(..., help="Qualified or unqualified symbol name."),
    repo: str | None = typer.Option(None, help="Filter by repository in owner/name form."),
    branch: str | None = typer.Option(None, help="Filter by indexed git branch name."),
    limit: int = typer.Option(100, min=1, help="Maximum number of results to return."),
    db_path: Path | None = typer.Option(None, help="Override the SQLite database path."),
) -> None:
    """Show indexed callers for a symbol."""
    normalized_repo = _normalize_repo(repo)
    settings = _load_cli_settings(db_path)
    connection = DatabaseManager(settings.db_path).initialize()
    try:
        rows = get_symbol_callers(
            connection,
            symbol=symbol,
            repo=normalized_repo,
            branch=branch,
            limit=limit,
        )
        console.print(render_symbol_search(rows, title=f"Symbol callers · {symbol}"))
    finally:
        connection.close()


@code_app.command("callees")
def code_callees(
    symbol: str = typer.Argument(..., help="Qualified or unqualified symbol name."),
    repo: str | None = typer.Option(None, help="Filter by repository in owner/name form."),
    branch: str | None = typer.Option(None, help="Filter by indexed git branch name."),
    limit: int = typer.Option(100, min=1, help="Maximum number of results to return."),
    db_path: Path | None = typer.Option(None, help="Override the SQLite database path."),
) -> None:
    """Show indexed outgoing calls made by a symbol."""
    normalized_repo = _normalize_repo(repo)
    settings = _load_cli_settings(db_path)
    connection = DatabaseManager(settings.db_path).initialize()
    try:
        rows = get_symbol_callees(
            connection,
            symbol=symbol,
            repo=normalized_repo,
            branch=branch,
            limit=limit,
        )
        mapped_rows = []
        for r in rows:
            mapped = dict(r)
            mapped["qualified_name"] = r["callee"]
            mapped_rows.append(mapped)
        console.print(render_symbol_search(mapped_rows, title=f"Symbol callees · {symbol}"))
    finally:
        connection.close()


@code_app.command("impacted")
def code_impacted(
    symbol: str = typer.Argument(..., help="Qualified or unqualified symbol name."),
    repo: str | None = typer.Option(None, help="Filter by repository in owner/name form."),
    branch: str | None = typer.Option(None, help="Filter by indexed git branch name."),
    limit: int = typer.Option(50, min=1, help="Maximum number of files to return."),
    db_path: Path | None = typer.Option(None, help="Override the SQLite database path."),
) -> None:
    """Show files impacted by a symbol definition and its direct callers."""
    normalized_repo = _normalize_repo(repo)
    settings = _load_cli_settings(db_path)
    connection = DatabaseManager(settings.db_path).initialize()
    try:
        rows = get_impacted_files(
            connection,
            symbol=symbol,
            repo=normalized_repo,
            branch=branch,
            limit=limit,
        )
        console.print(render_impacted_files(rows, symbol=symbol))
    finally:
        connection.close()


@code_app.command("context")
def code_context(
    file_path: str = typer.Argument(..., help="Repo-relative or absolute file path to inspect."),
    repo: str | None = typer.Option(None, help="Filter by repository in owner/name form."),
    branch: str | None = typer.Option(None, help="Filter by indexed git branch name."),
    explain: bool = typer.Option(False, help="Show retrieval reasons and confidence levels."),
    cwd: Path | None = typer.Option(None, help="Workspace root for path normalization."),
    db_path: Path | None = typer.Option(None, help="Override the SQLite database path."),
) -> None:
    """Show combined code and review context for a file."""
    normalized_repo = _normalize_repo(repo)
    settings = _load_cli_settings(db_path)
    connection = DatabaseManager(settings.db_path).initialize()
    try:
        payload = get_combined_file_context(
            connection,
            file_path=file_path,
            repo=normalized_repo,
            branch=branch,
            cwd=cwd,
            explain=explain,
        )
        console.print(render_file_context_report(payload))
    finally:
        connection.close()


@doctor_app.command("retrieval")
def doctor_retrieval(
    db_path: Path | None = typer.Option(None, help="Override the SQLite database path."),
) -> None:
    """Inspect retrieval-quality issues such as stale links and orphaned file references."""
    settings = _load_cli_settings(db_path)
    connection = DatabaseManager(settings.db_path).initialize()
    try:
        console.print(render_retrieval_doctor(run_retrieval_doctor(connection)))
    finally:
        connection.close()


@review_app.command("ready")
def review_ready(
    repo: str | None = typer.Option(None, help="Repository in owner/name form."),
    pr: int | None = typer.Option(None, help="Pull request number."),
    cwd: Path | None = typer.Option(None, help="Git working tree to inspect for branch defaults."),
    no_gh_fallback: bool = typer.Option(
        False,
        help="Skip GitHub CLI fallback when resolving the current branch PR.",
    ),
    stale_days: int = typer.Option(3, min=0, help="Staleness threshold for follow-up suggestions."),
    db_path: Path | None = typer.Option(None, help="Override the SQLite database path."),
) -> None:
    """Summarize what remains before a PR is likely ready to merge."""
    normalized_repo = _normalize_repo(repo)
    settings = _load_cli_settings(db_path)
    connection = DatabaseManager(settings.db_path).initialize()
    try:
        resolved_repo = normalized_repo
        resolved_pr = pr
        if resolved_pr is None:
            resolved = resolve_branch_pr(
                connection,
                cwd=cwd,
                repo=normalized_repo,
                allow_gh_fallback=not no_gh_fallback,
            )
            resolved_repo = resolved["repo"]
            resolved_pr = resolved["pr_number"]
        elif resolved_repo is None:
            worktree = get_git_worktree(cwd)
            resolved_repo = worktree["repo"]
        assert resolved_repo is not None
        assert resolved_pr is not None
        payload = get_merge_readiness_payload(
            connection,
            repo=resolved_repo,
            pr_number=resolved_pr,
            stale_days=stale_days,
        )
        console.print(render_merge_readiness(payload))
    except BranchContextError as exc:
        _fail_branch_command(exc)
    finally:
        connection.close()


@branch_app.command("current-pr")
def branch_current_pr(
    repo: str | None = typer.Option(None, help="Override the detected GitHub repo."),
    branch: str | None = typer.Option(None, help="Override the current branch name."),
    cwd: Path | None = typer.Option(None, help="Git working tree to inspect."),
    db_path: Path | None = typer.Option(None, help="Override the SQLite database path."),
) -> None:
    """Resolve the current branch to its pull request."""
    normalized_repo = _normalize_repo(repo)
    settings = _load_cli_settings(db_path)
    connection = DatabaseManager(settings.db_path).initialize()
    try:
        payload = resolve_branch_pr(connection, cwd=cwd, repo=normalized_repo, branch_name=branch)
        console.print(render_branch_resolution(payload))
    except BranchContextError as exc:
        _fail_branch_command(exc)
    finally:
        connection.close()

@branch_app.command("design")
def branch_design(
    db_path: Path | None = typer.Option(None, help="Override the SQLite database path."),
) -> None:
    """Show architectural design memory for the current branch's PR."""
    import json
    settings = _load_cli_settings(db_path)
    cwd = Path.cwd()
    connection = DatabaseManager(settings.db_path).initialize()
    try:
        payload = resolve_branch_pr(connection, cwd=cwd)
        repo_slug = payload["repo"]
        pr_number = payload["pr_number"]
        repo_row = connection.execute("SELECT id FROM repos WHERE owner = ? AND name = ?", tuple(repo_slug.split("/"))).fetchone()  # noqa: E501
        if not repo_row:
            console.print(f"Repository {repo_slug} not found.")
            raise typer.Exit(code=1)
        
        memory = asyncio.run(architecture.generate_architectural_memory(
            connection, "pr", pr_number, repo_row["id"]
        ))
        console.print(f"\n[bold cyan]Architectural Design Memory for {repo_slug}#{pr_number}[/bold cyan]")  # noqa: E501
        console.print(json.dumps(memory, indent=2))
    except BranchContextError as exc:
        _fail_branch_command(exc)
    finally:
        connection.close()


@branch_app.command()
def context(
    repo: str | None = typer.Option(None, help="Override the detected GitHub repo."),
    branch: str | None = typer.Option(None, help="Override the current branch name."),
    cwd: Path | None = typer.Option(None, help="Git working tree to inspect."),
    no_gh_fallback: bool = typer.Option(
        False,
        help="Skip GitHub CLI fallback and resolve only from local metadata and synced state.",
    ),
    explain: bool = typer.Option(False, help="Show retrieval reasons and confidence levels."),
    fail_on_blocking: bool = typer.Option(
        False,
        help="Exit with code 10 when the resolved PR still has blocking threads.",
    ),
    quiet: bool = typer.Option(False, help="Suppress normal output and use exit codes only."),
    db_path: Path | None = typer.Option(None, help="Override the SQLite database path."),
) -> None:
    """Show the branch-aware PR context for the current worktree."""
    normalized_repo = _normalize_repo(repo)
    settings = _load_cli_settings(db_path)
    connection = DatabaseManager(settings.db_path).initialize()
    try:
        payload = get_branch_context_payload(
            connection,
            cwd=cwd,
            repo=normalized_repo,
            branch_name=branch,
            allow_gh_fallback=not no_gh_fallback,
            explain=explain,
        )
        if not quiet:
            console.print(render_branch_context(payload))
        blocking_threads = payload["pr_context"]["health"]["blocking_threads"]
        if fail_on_blocking and blocking_threads > 0:
            raise typer.Exit(code=10)
    except BranchContextError as exc:
        _fail_branch_command(exc, quiet=quiet)
    finally:
        connection.close()


@branch_app.command("file-context")
def branch_file_context(
    file_path: str = typer.Argument(..., help="File path to inspect within the current repo."),
    repo: str | None = typer.Option(None, help="Override the detected GitHub repo."),
    branch: str | None = typer.Option(None, help="Override the current branch name."),
    cwd: Path | None = typer.Option(None, help="Git working tree to inspect."),
    no_gh_fallback: bool = typer.Option(
        False,
        help="Skip GitHub CLI fallback and resolve only from local metadata and synced state.",
    ),
    explain: bool = typer.Option(False, help="Show retrieval reasons and confidence levels."),
    open_only: bool = typer.Option(
        False, "--open-only", help="Exclude resolved review history from the output."
    ),
    db_path: Path | None = typer.Option(None, help="Override the SQLite database path."),
) -> None:
    """Show review context for a file on the current branch PR."""
    normalized_repo = _normalize_repo(repo)
    settings = _load_cli_settings(db_path)
    connection = DatabaseManager(settings.db_path).initialize()
    try:
        payload = get_branch_file_context(
            connection,
            file_path=file_path,
            cwd=cwd,
            repo=normalized_repo,
            branch_name=branch,
            allow_gh_fallback=not no_gh_fallback,
            explain=explain,
            open_only=open_only,
        )
        console.print(render_branch_file_context(payload))
    except (BranchContextError, ValueError) as exc:
        _fail_branch_command(exc)
    finally:
        connection.close()


@branch_app.command()
def link(
    pr: int = typer.Option(..., help="Pull request number to associate with the branch."),
    repo: str | None = typer.Option(None, help="Override the detected GitHub repo."),
    branch: str | None = typer.Option(None, help="Override the current branch name."),
    cwd: Path | None = typer.Option(None, help="Git working tree to inspect."),
    db_path: Path | None = typer.Option(None, help="Override the SQLite database path."),
) -> None:
    """Manually link the current branch to a synced PR."""
    normalized_repo = _normalize_repo(repo)
    settings = _load_cli_settings(db_path)
    connection = DatabaseManager(settings.db_path).initialize()
    try:
        worktree = get_git_worktree(cwd)
        resolved_repo = normalized_repo or worktree["repo"]
        resolved_branch = branch or worktree["branch"]
        if resolved_repo is None:
            raise BranchContextError(
                "Could not determine the GitHub repo. Pass --repo in owner/name form."
            )
        link_branch_to_pr(
            connection,
            repo=resolved_repo,
            branch_name=resolved_branch,
            pr_number=pr,
        )
        console.print(
            render_branch_resolution(
                {
                    "repo": resolved_repo,
                    "branch": resolved_branch,
                    "pr_number": pr,
                    "source": "manual_link",
                    "repo_root": str(worktree["repo_root"]),
                }
            )
        )
    except BranchContextError as exc:
        _fail_branch_command(exc)
    finally:
        connection.close()

@code_app.command("why")
def code_why(
    file_path: str = typer.Argument(..., help="File path to explain."),
    repo: str | None = typer.Option(None, help="Repository in owner/name form."),
    cwd: Path | None = typer.Option(None, help="Git working tree."),
    db_path: Path | None = typer.Option(None, help="Override the SQLite database path."),
) -> None:
    """Explain why a file looks the way it does using architectural memory."""
    settings = _load_cli_settings(db_path)
    connection = DatabaseManager(settings.db_path).initialize()
    try:
        worktree = get_git_worktree(cwd)
        resolved_repo = repo or worktree["repo"]
        if not resolved_repo:
            console.print("[red]Could not determine repository.[/red]")
            raise typer.Exit(1)
            
        repo_row = connection.execute("SELECT id FROM repos WHERE owner = ? AND name = ?", tuple(resolved_repo.split("/"))).fetchone()  # noqa: E501
        if not repo_row:
            console.print(f"Repository {resolved_repo} not found.")
            raise typer.Exit(1)
            
        explanation = asyncio.run(architecture.explain_code(
            connection, repo_row["id"], resolved_repo, file_path
        ))
        console.print(f"\n[bold cyan]Explanation for {file_path}[/bold cyan]")
        console.print(explanation)
    finally:
        connection.close()

@app.command("install-hooks")
def install_hooks(
    cwd: Path | None = typer.Option(None, help="Git working tree to install hooks into."),
) -> None:
    """Install warning-only git hooks for branch-aware review reminders."""
    try:
        repo_root = get_git_repo_root(cwd)
        installed = install_git_hooks(repo_root)
    except (BranchContextError, HookInstallError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(render_hook_installation([str(path) for path in installed]))


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


open_app = typer.Typer(help="Open references (e.g. discussions) in the browser or terminal.")
app.add_typer(open_app, name="open")


@open_app.command("discussion")
def open_discussion(
    number: int = typer.Argument(..., help="The discussion number to open."),
    repo: str | None = typer.Option(None, help="The GitHub repository slug."),
    db_path: Path | None = typer.Option(None, help="Override the SQLite database path."),
) -> None:
    """Open a tracked discussion link in the browser or print its URL."""
    settings = _load_cli_settings(db_path)
    connection = DatabaseManager(settings.db_path).initialize()
    try:
        query_sql = (
            "SELECT url, target_repo, title FROM extracted_references "
            "WHERE reference_kind = 'discussion' AND target_number = ?"
        )
        params: list[object] = [number]
        if repo:
            query_sql += " AND target_repo = ?"
            params.append(repo)
        
        rows = connection.execute(query_sql, params).fetchall()
        if not rows:
            if repo:
                url = f"https://github.com/{repo}/discussions/{number}"
                title = f"Discussion #{number}"
            else:
                repos = connection.execute(
                    "SELECT owner || '/' || name AS slug FROM repos"
                ).fetchall()
                if len(repos) == 1:
                    repo_slug = repos[0]["slug"]
                    url = f"https://github.com/{repo_slug}/discussions/{number}"
                    title = f"Discussion #{number}"
                elif len(repos) > 1:
                    console.print(
                        f"[red]Error: Discussion #{number} not found in database, "
                        "and multiple repos are tracked. Please specify --repo.[/red]"
                    )
                    raise typer.Exit(code=1)
                else:
                    console.print(
                        f"[red]Error: Discussion #{number} not found in database, "
                        "and no repo was specified.[/red]"
                    )
                    raise typer.Exit(code=1)
        elif len(rows) > 1:
            console.print(
                f"[red]Error: Discussion #{number} found in multiple repos. "
                "Please specify --repo to disambiguate.[/red]"
            )
            raise typer.Exit(code=1)
        else:
            row = rows[0]
            url = row["url"]
            title = row["title"] or f"Discussion #{number}"

        console.print(f"Opening [bold cyan]{title}[/bold cyan]...")
        console.print(f"URL: [blue]{url}[/blue]")
        
        import webbrowser
        webbrowser.open(url)
    finally:
        connection.close()


if __name__ == "__main__":
    app()
