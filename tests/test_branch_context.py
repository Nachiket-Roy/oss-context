"""Tests for Phase 4 branch-aware context resolution.

This file verifies git remote parsing, branch-to-PR resolution from synced data
and manual links, and file-scoped unresolved thread filtering for the current
branch workflow.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from oss_context.branch_context import (
    get_branch_file_context,
    link_branch_to_pr,
    parse_github_remote,
    resolve_branch_pr,
)
from oss_context.db import DatabaseManager


def _fake_runner_factory(repo_root: Path, branch: str, remote_url: str):
    def runner(args: list[str], cwd: Path | None, allow_failure: bool):
        if args == ["git", "rev-parse", "--show-toplevel"]:
            return str(repo_root)
        if args == ["git", "branch", "--show-current"]:
            return branch
        if args == ["git", "remote", "get-url", "origin"]:
            return remote_url
        if args[:4] == ["gh", "pr", "view", "--json"]:
            return None if allow_failure else ""
        raise AssertionError(f"Unexpected command: {args}")

    return runner


def _seed_branch_fixture(connection) -> None:
    now = datetime.now(UTC)
    connection.execute(
        (
            "INSERT INTO repos(id, github_id, owner, name, default_branch, last_synced_at) "
            "VALUES(1, 10, 'acme', 'widgets', 'main', ?)"
        ),
        (now.isoformat(),),
    )
    connection.execute(
        """
        INSERT INTO prs(
            id, github_id, repo_id, number, title, state, author,
            created_at, updated_at, body, base_branch, head_branch, merge_commit_sha
        )
        VALUES(
            1, 101, 1, 42, 'Auth', 'open', 'alice',
            ?, ?, 'body', 'main', 'feature/auth', NULL
        )
        """,
        ((now - timedelta(days=1)).isoformat(), now.isoformat()),
    )
    connection.execute(
        """
        INSERT INTO review_threads(
            id, github_thread_id, pr_id, file_path, line_number,
            thread_state, resolved_by, resolved_at, created_at, updated_at
        )
        VALUES(1, 'thread-1', 1, 'src/auth.py', 42, 'active', NULL, NULL, ?, ?)
        """,
        ((now - timedelta(days=1)).isoformat(), now.isoformat()),
    )
    connection.execute(
        """
        INSERT INTO review_threads(
            id, github_thread_id, pr_id, file_path, line_number,
            thread_state, resolved_by, resolved_at, created_at, updated_at
        )
        VALUES(2, 'thread-2', 1, 'tests/test_auth.py', 11, 'active', NULL, NULL, ?, ?)
        """,
        ((now - timedelta(hours=10)).isoformat(), now.isoformat()),
    )
    connection.execute(
        """
        INSERT INTO review_comments(
            id, thread_id, github_comment_id, author, body, created_at, updated_at,
            reaction_count, is_suggestion, suggestion_applied,
            extracted_decision, decision_confidence
        ) VALUES(
            1, 1, 5001, 'bob', 'Please fix auth.',
            ?, ?, 0, 0, 0, 'REQUEST_CHANGES', 0.91
        )
        """,
        ((now - timedelta(days=1)).isoformat(), (now - timedelta(days=1)).isoformat()),
    )
    connection.execute(
        """
        INSERT INTO review_comments(
            id, thread_id, github_comment_id, author, body, created_at, updated_at,
            reaction_count, is_suggestion, suggestion_applied,
            extracted_decision, decision_confidence
        ) VALUES(
            2, 2, 5002, 'carol', 'Can you add another test?',
            ?, ?, 0, 0, 0, 'QUESTION', 0.67
        )
        """,
        ((now - timedelta(hours=10)).isoformat(), (now - timedelta(hours=10)).isoformat()),
    )
    connection.execute(
        """
        INSERT INTO llm_cache(
            comment_id, provider, model, input_hash,
            decision_type, summary, confidence, analyzed_at
        ) VALUES(
            1, 'heuristic', 'heuristic-v1', 'hash-1',
            'REQUEST_CHANGES', 'Blocking auth fix', 0.91, ?
        )
        """,
        (now.isoformat(),),
    )
    connection.execute(
        """
        INSERT INTO llm_cache(
            comment_id, provider, model, input_hash,
            decision_type, summary, confidence, analyzed_at
        ) VALUES(
            2, 'heuristic', 'heuristic-v1', 'hash-2',
            'QUESTION', 'Need another test', 0.67, ?
        )
        """,
        (now.isoformat(),),
    )
    connection.commit()


def test_parse_github_remote_supports_https_and_ssh():
    assert parse_github_remote("https://github.com/acme/widgets.git") == "acme/widgets"
    assert parse_github_remote("git@github.com:acme/widgets.git") == "acme/widgets"
    assert parse_github_remote("https://gitlab.com/acme/widgets.git") is None


def test_resolve_branch_pr_from_synced_head_branch(tmp_path):
    connection = DatabaseManager(tmp_path / "oss_context.db").initialize()
    try:
        _seed_branch_fixture(connection)
        runner = _fake_runner_factory(
            tmp_path,
            "feature/auth",
            "https://github.com/acme/widgets.git",
        )
        resolved = resolve_branch_pr(connection, runner=runner)
        assert resolved["repo"] == "acme/widgets"
        assert resolved["pr_number"] == 42
        assert resolved["source"] == "synced_branch"
        assert resolved["branch"] == "feature/auth"
    finally:
        connection.close()


def test_manual_branch_link_overrides_auto_resolution(tmp_path):
    connection = DatabaseManager(tmp_path / "oss_context.db").initialize()
    try:
        _seed_branch_fixture(connection)
        link_branch_to_pr(
            connection,
            repo="acme/widgets",
            branch_name="wip/auth-refactor",
            pr_number=42,
        )
        runner = _fake_runner_factory(
            tmp_path,
            "wip/auth-refactor",
            "https://github.com/acme/widgets.git",
        )
        resolved = resolve_branch_pr(connection, runner=runner)
        assert resolved["source"] == "manual_link"
        assert resolved["pr_number"] == 42
    finally:
        connection.close()


def test_branch_file_context_filters_threads_for_target_file(tmp_path):
    connection = DatabaseManager(tmp_path / "oss_context.db").initialize()
    try:
        _seed_branch_fixture(connection)
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        runner = _fake_runner_factory(
            repo_root,
            "feature/auth",
            "https://github.com/acme/widgets.git",
        )
        payload = get_branch_file_context(
            connection,
            file_path="auth.py",
            cwd=repo_root,
            runner=runner,
        )
        assert payload["repo"] == "acme/widgets"
        assert payload["pr_number"] == 42
        assert payload["file_path"] == "auth.py"
        assert len(payload["threads"]) == 1
        assert payload["threads"][0]["file_path"] == "src/auth.py"
    finally:
        connection.close()
