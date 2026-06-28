"""Branch-aware context resolution for oss-context.

This module bridges the local git worktree to synced pull-request state. It can
inspect the current repository and branch, resolve the associated PR from a
manual link, the local SQLite graph, or the GitHub CLI, and assemble branch-
aware PR and file-level context for local development workflows.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from oss_context.models import RepoRef
from oss_context.queries import (
    get_pr_context_payload,
    list_resolved_pr_decisions,
    list_unresolved_threads,
)
from oss_context.retrieval import build_provenance, summarize_provenance

CommandRunner = Callable[[list[str], Path | None, bool], str | None]
COMMAND_TIMEOUT_SECONDS = 5.0


class BranchContextError(RuntimeError):
    """Raised when branch-aware context cannot be resolved."""


def _run_command(
    args: list[str],
    cwd: Path | None = None,
    allow_failure: bool = False,
) -> str | None:
    """Run a local command and return stdout, optionally tolerating failures."""
    try:
        completed = subprocess.run(
            args,
            cwd=str(cwd) if cwd is not None else None,
            text=True,
            capture_output=True,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        if allow_failure:
            return None
        raise BranchContextError(
            f"Command timed out after {COMMAND_TIMEOUT_SECONDS:.0f}s: {' '.join(args)}"
        ) from exc
    if completed.returncode != 0:
        if allow_failure:
            return None
        stderr = completed.stderr.strip() or completed.stdout.strip() or "command failed"
        raise BranchContextError(stderr)
    return completed.stdout.strip()


def parse_github_remote(url: str | None) -> str | None:
    """Parse a GitHub remote URL into owner/name form when possible."""
    if not url:
        return None
    normalized = url.strip()
    for prefix in ("git@github.com:", "ssh://git@github.com/"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    else:
        for prefix in (
            "https://github.com/",
            "http://github.com/",
            "git://github.com/",
        ):
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix) :]
                break
        else:
            return None

    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    try:
        return RepoRef.from_slug(normalized).slug
    except ValueError:
        return None


def get_git_repo_root(
    cwd: Path | None = None,
    *,
    runner: CommandRunner = _run_command,
) -> Path:
    """Resolve the git repository root for a working tree."""
    repo_root_raw = runner(["git", "rev-parse", "--show-toplevel"], cwd, False)
    assert repo_root_raw is not None
    return Path(repo_root_raw)


def get_git_worktree(
    cwd: Path | None = None,
    *,
    runner: CommandRunner = _run_command,
) -> dict[str, Any]:
    """Inspect the current git worktree for repo root, branch, and GitHub repo."""
    repo_root = get_git_repo_root(cwd, runner=runner)
    branch = runner(["git", "branch", "--show-current"], repo_root, False)
    if not branch:
        raise BranchContextError("Could not determine the current git branch.")
    remote_url = runner(["git", "remote", "get-url", "origin"], repo_root, True)
    return {
        "repo_root": repo_root,
        "branch": branch,
        "remote_url": remote_url,
        "repo": parse_github_remote(remote_url),
    }


def _get_manual_link(
    connection: sqlite3.Connection,
    *,
    branch_name: str,
    repo: str | None,
) -> dict[str, Any] | None:
    """Resolve a manual branch link from SQLite."""
    if repo is not None:
        row = connection.execute(
            """
            SELECT repo_slug, pr_number, linked_at
            FROM branch_links
            WHERE repo_slug = ? AND branch_name = ?
            """,
            (repo, branch_name),
        ).fetchone()
        if row is not None:
            return {
                "repo": row["repo_slug"],
                "pr_number": row["pr_number"],
                "source": "manual_link",
                "linked_at": row["linked_at"],
            }
        return None

    rows = connection.execute(
        """
        SELECT repo_slug, pr_number, linked_at
        FROM branch_links
        WHERE branch_name = ?
        ORDER BY linked_at DESC
        """,
        (branch_name,),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise BranchContextError(
            f"Branch {branch_name!r} is linked to multiple repos. Pass --repo to disambiguate."
        )
    row = rows[0]
    return {
        "repo": row["repo_slug"],
        "pr_number": row["pr_number"],
        "source": "manual_link",
        "linked_at": row["linked_at"],
    }


def _find_pr_by_synced_branch(
    connection: sqlite3.Connection,
    *,
    repo: str,
    branch_name: str,
) -> dict[str, Any] | None:
    """Find a PR in the synced database whose head branch matches the current branch."""
    repo_ref = RepoRef.from_slug(repo)
    rows = connection.execute(
        """
        SELECT p.number, p.title, p.state, p.updated_at
        FROM prs p
        JOIN repos r ON r.id = p.repo_id
        WHERE r.owner = ? AND r.name = ? AND p.head_branch = ?
        ORDER BY CASE WHEN p.state = 'open' THEN 0 ELSE 1 END, p.updated_at DESC, p.id DESC
        LIMIT 10
        """,
        (repo_ref.owner, repo_ref.name, branch_name),
    ).fetchall()
    if not rows:
        return None

    open_rows = [row for row in rows if row["state"] == "open"]
    if len(open_rows) == 1:
        row = open_rows[0]
    elif len(open_rows) > 1:
        raise BranchContextError(
            f"Multiple open PRs match branch {branch_name!r} in {repo}. Use branch link to pin one."
        )
    elif len(rows) == 1:
        row = rows[0]
    else:
        raise BranchContextError(
            f"Multiple synced PRs match branch {branch_name!r} in {repo}. "
            "Use branch link to pin one."
        )
    return {
        "repo": repo,
        "pr_number": row["number"],
        "title": row["title"],
        "state": row["state"],
        "source": "synced_branch",
    }


def _parse_github_pr_url(url: str | None) -> tuple[str, int] | None:
    """Parse a GitHub pull request URL into repo slug and PR number."""
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.netloc and parsed.netloc.lower() != "github.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4 or parts[2] != "pull":
        return None
    try:
        repo = RepoRef.from_slug(f"{parts[0]}/{parts[1]}").slug
        pr_number = int(parts[3])
    except (ValueError, TypeError):
        return None
    return repo, pr_number


def _find_pr_with_gh(
    *,
    repo_root: Path,
    runner: CommandRunner,
    expected_repo: str | None,
    expected_branch: str | None,
) -> dict[str, Any] | None:
    """Fallback to `gh pr view` when the local SQLite graph cannot resolve the branch."""
    output = runner(
        [
            "gh",
            "pr",
            "view",
            "--json",
            "number,title,state,url,headRefName,baseRefName",
        ],
        repo_root,
        True,
    )
    if not output:
        return None
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise BranchContextError("gh returned invalid PR JSON.") from exc

    parsed_url = _parse_github_pr_url(payload.get("url"))
    if parsed_url is None:
        return None
    repo, pr_number = parsed_url
    if expected_repo is not None and repo != expected_repo:
        return None
    head_branch = payload.get("headRefName")
    if expected_branch is not None and head_branch != expected_branch:
        return None
    return {
        "repo": repo,
        "pr_number": pr_number,
        "title": payload.get("title"),
        "state": payload.get("state"),
        "source": "gh_cli",
    }


def resolve_branch_pr(
    connection: sqlite3.Connection,
    *,
    cwd: Path | None = None,
    repo: str | None = None,
    branch_name: str | None = None,
    allow_gh_fallback: bool = True,
    runner: CommandRunner = _run_command,
) -> dict[str, Any]:
    """Resolve the pull request associated with the current branch."""
    worktree = get_git_worktree(cwd, runner=runner)
    resolved_branch = branch_name or worktree["branch"]

    resolved_repo = repo
    if resolved_repo is None:
        detected_repo = None
        try:
            remotes_str = runner(["git", "remote"], worktree["repo_root"], True)
            remotes = (
                [r.strip() for r in remotes_str.splitlines() if r.strip()]
                if remotes_str
                else ["origin"]
            )
        except Exception:
            remotes = ["origin"]

        remote_slugs = {}
        for r_name in remotes:
            try:
                r_url = runner(
                    ["git", "remote", "get-url", r_name],
                    worktree["repo_root"],
                    True,
                )
                if r_url:
                    slug = parse_github_remote(r_url)
                    if slug:
                        remote_slugs[r_name] = slug
            except Exception:
                pass

        rows = connection.execute("SELECT owner, name FROM repos").fetchall()
        db_slugs = {f"{row['owner']}/{row['name']}" for row in rows}

        if "upstream" in remote_slugs and remote_slugs["upstream"] in db_slugs:
            detected_repo = remote_slugs["upstream"]
        elif "origin" in remote_slugs and remote_slugs["origin"] in db_slugs:
            detected_repo = remote_slugs["origin"]
        else:
            for slug in remote_slugs.values():
                if slug in db_slugs:
                    detected_repo = slug
                    break
        resolved_repo = detected_repo or worktree["repo"]

    manual_link = _get_manual_link(connection, branch_name=resolved_branch, repo=repo)
    if manual_link is not None:
        return {
            "repo_root": worktree["repo_root"],
            "branch": resolved_branch,
            **manual_link,
        }

    if resolved_repo is not None:
        db_match = _find_pr_by_synced_branch(
            connection,
            repo=resolved_repo,
            branch_name=resolved_branch,
        )
        if db_match is not None:
            return {
                "repo_root": worktree["repo_root"],
                "branch": resolved_branch,
                **db_match,
            }

    can_use_gh_fallback = allow_gh_fallback and (
        branch_name is None and (repo is None or repo == worktree["repo"])
    )
    if can_use_gh_fallback:
        gh_match = _find_pr_with_gh(
            repo_root=worktree["repo_root"],
            runner=runner,
            expected_repo=resolved_repo,
            expected_branch=resolved_branch,
        )
    else:
        gh_match = None
    if gh_match is not None:
        return {
            "repo_root": worktree["repo_root"],
            "branch": resolved_branch,
            **gh_match,
        }

    raise BranchContextError(
        "Could not resolve a PR for the current branch. "
        "Sync the repo first or use `oss-context branch link`."
    )


def link_branch_to_pr(
    connection: sqlite3.Connection,
    *,
    repo: str,
    branch_name: str,
    pr_number: int,
) -> None:
    """Persist a manual branch-to-PR association."""
    repo_ref = RepoRef.from_slug(repo)
    row = connection.execute(
        """
        SELECT 1
        FROM prs p
        JOIN repos r ON r.id = p.repo_id
        WHERE r.owner = ? AND r.name = ? AND p.number = ?
        """,
        (repo_ref.owner, repo_ref.name, pr_number),
    ).fetchone()
    if row is None:
        raise BranchContextError(f"PR #{pr_number} has not been synced for {repo}.")

    connection.execute(
        """
        INSERT INTO branch_links(repo_slug, branch_name, pr_number, linked_at)
        VALUES(?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(repo_slug, branch_name) DO UPDATE SET
            pr_number = excluded.pr_number,
            linked_at = CURRENT_TIMESTAMP
        """,
        (repo_ref.slug, branch_name, pr_number),
    )
    connection.commit()


def _normalize_file_for_repo(file_path: str, repo_root: Path) -> str:
    """Normalize an input path to a repo-relative POSIX-like path."""
    raw = Path(file_path)
    resolved_root = repo_root.resolve()
    try:
        if raw.is_absolute():
            normalized = raw.resolve().relative_to(resolved_root)
        else:
            normalized = (resolved_root / raw).resolve().relative_to(resolved_root)
    except ValueError as exc:
        raise BranchContextError(
            f"File path {file_path!r} is outside the repository root {repo_root}."
        ) from exc
    return normalized.as_posix()


def _file_matches(candidate: str, expected: str) -> bool:
    """Match repo-relative file paths using exact or suffix semantics."""
    normalized_candidate = candidate.replace("\\", "/")
    normalized_expected = expected.replace("\\", "/")
    return (
        normalized_candidate == normalized_expected
        or normalized_candidate.endswith(f"/{normalized_expected}")
        or normalized_expected.endswith(f"/{normalized_candidate}")
    )


def get_branch_context_payload(
    connection: sqlite3.Connection,
    *,
    cwd: Path | None = None,
    repo: str | None = None,
    branch_name: str | None = None,
    allow_gh_fallback: bool = True,
    runner: CommandRunner = _run_command,
    explain: bool = False,
) -> dict[str, Any]:
    """Assemble branch-aware PR context for the current worktree."""
    resolved = resolve_branch_pr(
        connection,
        cwd=cwd,
        repo=repo,
        branch_name=branch_name,
        allow_gh_fallback=allow_gh_fallback,
        runner=runner,
    )
    pr_context = get_pr_context_payload(
        connection,
        repo=resolved["repo"],
        pr_number=resolved["pr_number"],
    )
    resolution_reason = {
        "manual_link": "Manual branch-to-PR link matched the current branch.",
        "synced_branch": "Exact branch name matched a synced PR head branch.",
        "gh_cli": "GitHub CLI returned the PR for the current branch.",
    }.get(resolved["source"], "Resolved from branch-aware PR context.")
    branch_provenance = build_provenance(
        source_type="branch_pr_resolution",
        source_id=f"{resolved['repo']}:{resolved['branch']}:{resolved['pr_number']}",
        confidence="HIGH",
        retrieval_reason="exact_branch_mapping",
        reason_detail=resolution_reason,
    )
    return {
        "repo": resolved["repo"],
        "pr_number": resolved["pr_number"],
        "branch": resolved["branch"],
        "repo_root": str(resolved["repo_root"]),
        "resolution_source": resolved["source"],
        "pr_context": pr_context,
        "explain": explain,
        "provenance": branch_provenance,
        "retrieval_explanations": summarize_provenance([{"provenance": branch_provenance}]),
    }


def get_branch_file_context(
    connection: sqlite3.Connection,
    *,
    file_path: str,
    cwd: Path | None = None,
    repo: str | None = None,
    branch_name: str | None = None,
    allow_gh_fallback: bool = True,
    runner: CommandRunner = _run_command,
    explain: bool = False,
    open_only: bool = False,
) -> dict[str, Any]:
    """Assemble file-scoped review context for the current branch PR."""
    branch_context = get_branch_context_payload(
        connection,
        cwd=cwd,
        repo=repo,
        branch_name=branch_name,
        allow_gh_fallback=allow_gh_fallback,
        runner=runner,
        explain=explain,
    )
    relative_path = _normalize_file_for_repo(file_path, Path(branch_context["repo_root"]))
    all_threads = [
        row
        for row in list_unresolved_threads(connection, repo=branch_context["repo"])
        if row["pr_number"] == branch_context["pr_number"]
    ]
    matching_threads = [
        {
            **row,
            "provenance": build_provenance(
                source_type="review_thread",
                source_id=str(row["thread_id"]),
                confidence="HIGH",
                retrieval_reason="exact_file_match",
                reason_detail=f"Active review thread matches {row['file_path']}",
            ),
        }
        for row in all_threads
        if row["file_path"] not in {None, "—"} and _file_matches(row["file_path"], relative_path)
    ]
    references = [
        {
            **reference,
            "provenance": build_provenance(
                source_type=(
                    "pr_reference" if reference["source_kind"] == "pr" else "review_reference"
                ),
                source_id=str(reference["raw_text"]),
                confidence="HIGH",
                retrieval_reason="explicit_issue_reference",
                reason_detail="Explicit reference found in the current PR context.",
            ),
        }
        for reference in branch_context["pr_context"]["references"]
        if reference.get("target_number") is not None or reference.get("url")
    ]
    
    resolved_history = []
    if not open_only:
        all_resolved = list_resolved_pr_decisions(
            connection,
            repo=branch_context["repo"],
            pr_number=branch_context["pr_number"],
        )
        resolved_history = [
            {
                **row,
                "provenance": build_provenance(
                    source_type="resolved_decision",
                    source_id=str(row["raw_text"]),
                    confidence="HIGH",
                    retrieval_reason="exact_file_match",
                    reason_detail=f"Resolved decision thread matches {row['file_path']}",
                ),
            }
            for row in all_resolved
            if row["file_path"] not in {None, "—"} 
            and _file_matches(row["file_path"], relative_path)
        ]

    provenance_items = [
        {"provenance": branch_context["provenance"]},
        *matching_threads,
        *resolved_history,
        *references,
    ]
    return {
        "repo": branch_context["repo"],
        "pr_number": branch_context["pr_number"],
        "branch": branch_context["branch"],
        "repo_root": branch_context["repo_root"],
        "resolution_source": branch_context["resolution_source"],
        "file_path": relative_path,
        "threads": matching_threads,
        "resolved_history": resolved_history,
        "references": references,
        "explain": explain,
        "provenance": branch_context["provenance"],
        "retrieval_explanations": summarize_provenance(provenance_items),
        "excluded": ["Semantic retrieval is not enabled; only deterministic context was returned."],
    }

