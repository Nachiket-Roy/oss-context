"""Branch-aware context resolution for oss-context.

This module bridges the local git worktree to synced pull-request state. It can
inspect the current repository and branch, resolve the associated PR from a
manual link, the local SQLite graph, or the GitHub CLI, and assemble branch-
aware PR and file-level context for Phase 4 workflows.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from oss_context.models import RepoRef
from oss_context.queries import get_pr_context_payload, list_unresolved_threads

CommandRunner = Callable[[list[str], Path | None, bool], str | None]


class BranchContextError(RuntimeError):
    """Raised when branch-aware context cannot be resolved."""


def _run_command(
    args: list[str],
    cwd: Path | None = None,
    allow_failure: bool = False,
) -> str | None:
    """Run a local command and return stdout, optionally tolerating failures."""
    completed = subprocess.run(
        args,
        cwd=str(cwd) if cwd is not None else None,
        text=True,
        capture_output=True,
        check=False,
    )
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


def get_git_worktree(
    cwd: Path | None = None,
    *,
    runner: CommandRunner = _run_command,
) -> dict[str, Any]:
    """Inspect the current git worktree for repo root, branch, and GitHub repo."""
    repo_root_raw = runner(["git", "rev-parse", "--show-toplevel"], cwd, False)
    assert repo_root_raw is not None
    repo_root = Path(repo_root_raw)
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
        if row is None:
            return None
        return {
            "repo": row["repo_slug"],
            "pr_number": row["pr_number"],
            "source": "manual_link",
            "linked_at": row["linked_at"],
        }

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
        LIMIT 2
        """,
        (repo_ref.owner, repo_ref.name, branch_name),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1 and rows[0]["number"] != rows[1]["number"]:
        raise BranchContextError(
            f"Multiple synced PRs match branch {branch_name!r} in {repo}. "
            "Use branch link to pin one."
        )
    row = rows[0]
    return {
        "repo": repo,
        "pr_number": row["number"],
        "title": row["title"],
        "state": row["state"],
        "source": "synced_branch",
    }


def _find_pr_with_gh(
    *,
    repo_root: Path,
    runner: CommandRunner,
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

    repo = None
    url = payload.get("url")
    if isinstance(url, str):
        repo = parse_github_remote(url.removeprefix("https://github.com/"))
        if repo is None and "github.com/" in url:
            repo = parse_github_remote(
                url.split("github.com/", maxsplit=1)[1].split("/pull/", maxsplit=1)[0]
            )
    if repo is None:
        return None
    number = payload.get("number")
    if not isinstance(number, int):
        return None
    return {
        "repo": repo,
        "pr_number": number,
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
    runner: CommandRunner = _run_command,
) -> dict[str, Any]:
    """Resolve the pull request associated with the current branch."""
    worktree = get_git_worktree(cwd, runner=runner)
    resolved_branch = branch_name or worktree["branch"]
    resolved_repo = repo or worktree["repo"]

    manual_link = _get_manual_link(connection, branch_name=resolved_branch, repo=resolved_repo)
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

    gh_match = _find_pr_with_gh(repo_root=worktree["repo_root"], runner=runner)
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
    if raw.is_absolute():
        normalized = raw.resolve().relative_to(repo_root.resolve())
    else:
        normalized = raw
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
    runner: CommandRunner = _run_command,
) -> dict[str, Any]:
    """Assemble branch-aware PR context for the current worktree."""
    resolved = resolve_branch_pr(
        connection,
        cwd=cwd,
        repo=repo,
        branch_name=branch_name,
        runner=runner,
    )
    pr_context = get_pr_context_payload(
        connection,
        repo=resolved["repo"],
        pr_number=resolved["pr_number"],
    )
    return {
        "repo": resolved["repo"],
        "pr_number": resolved["pr_number"],
        "branch": resolved["branch"],
        "repo_root": str(resolved["repo_root"]),
        "resolution_source": resolved["source"],
        "pr_context": pr_context,
    }


def get_branch_file_context(
    connection: sqlite3.Connection,
    *,
    file_path: str,
    cwd: Path | None = None,
    repo: str | None = None,
    branch_name: str | None = None,
    runner: CommandRunner = _run_command,
) -> dict[str, Any]:
    """Assemble file-scoped unresolved review context for the current branch PR."""
    branch_context = get_branch_context_payload(
        connection,
        cwd=cwd,
        repo=repo,
        branch_name=branch_name,
        runner=runner,
    )
    relative_path = _normalize_file_for_repo(file_path, Path(branch_context["repo_root"]))
    all_threads = [
        row
        for row in list_unresolved_threads(connection, repo=branch_context["repo"])
        if row["pr_number"] == branch_context["pr_number"]
    ]
    matching_threads = [
        row for row in all_threads if _file_matches(row["file_path"], relative_path)
    ]
    return {
        "repo": branch_context["repo"],
        "pr_number": branch_context["pr_number"],
        "branch": branch_context["branch"],
        "repo_root": branch_context["repo_root"],
        "resolution_source": branch_context["resolution_source"],
        "file_path": relative_path,
        "threads": matching_threads,
    }
