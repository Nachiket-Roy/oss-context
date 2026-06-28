"""Regression tests for previous scalability, correctness, and schema issues."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from oss_context.db import DatabaseManager
from oss_context.retrieval import run_retrieval_doctor
from oss_context.review_assistant import get_merge_readiness_payload


def test_redundant_index_is_dropped(tmp_path):
    """Verify the redundant idx_code_files_snapshot index is absent from schema."""
    conn = DatabaseManager(tmp_path / "oss_context.db").initialize()
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
            ("idx_code_files_snapshot",),
        )
        assert cursor.fetchone() is None
    finally:
        conn.close()


def test_retrieval_files_cache_memoization(tmp_path):
    """Verify diagnose_retrieval memoizes indexed file queries per repo."""
    conn = DatabaseManager(tmp_path / "oss_context.db").initialize()
    now = datetime.now(UTC)

    # Seed repository
    conn.execute(
        (
            "INSERT INTO repos(id, github_id, owner, name, default_branch, last_synced_at) "
            "VALUES(1, 10, 'acme', 'widgets', 'main', ?)"
        ),
        (now.isoformat(),),
    )
    # Seed snapshot
    conn.execute(
        (
            "INSERT INTO code_index_snapshots(id, repo_slug, repo_root, "
            "git_branch, git_commit, indexed_at) "
            "VALUES(1, 'acme/widgets', '/tmp', 'main', 'sha123', ?)"
        ),
        (now.isoformat(),),
    )
    # Seed indexed file
    conn.execute(
        
            "INSERT INTO code_index_files(id, snapshot_id, file_path, content_hash, language) "
            "VALUES(1, 1, 'service.py', 'hash1', 'python')"
        
    )
    # Seed PR
    conn.execute(
        """
        INSERT INTO prs(
            id, github_id, repo_id, number, title, state, author,
            created_at, updated_at, body, base_branch, head_branch, merge_commit_sha
        ) VALUES(
            1, 101, 1, 42, 'Auth', 'open', 'alice',
            ?, ?, 'body', 'main', 'feature/auth', NULL
        )
        """,
        ((now - timedelta(days=1)).isoformat(), now.isoformat()),
    )
    # Seed 3 review threads in the same repo/file
    for i in range(1, 4):
        conn.execute(
            """
            INSERT INTO review_threads(
                id, github_thread_id, pr_id, file_path, line_number,
                thread_state, resolved_by, resolved_at, created_at, updated_at
            ) VALUES(?, ?, 1, 'service.py', 6, 'active', NULL, NULL, ?, ?)
            """,
            (i, f"thread-{i}", (now - timedelta(hours=20)).isoformat(), now.isoformat()),
        )
        conn.execute(
            """
            INSERT INTO review_comments(
                id, thread_id, github_comment_id, author, body, created_at, updated_at,
                reaction_count, is_suggestion, suggestion_applied,
                extracted_decision, decision_confidence
            ) VALUES(?, ?, ?, 'bob', 'Please check this.', ?, ?, 0, 0, 0, 'COMMENT', 0.8)
            """,
            (
                i, i, 5000 + i,
                (now - timedelta(hours=20)).isoformat(),
                (now - timedelta(hours=20)).isoformat(),
            ),
        )
    conn.commit()

    # Intercept execute calls using a wrapper to count queries to code_index_files
    class ConnectionWrapper:
        def __init__(self, c):
            self.c = c
            self.query_count = 0

        def execute(self, sql, *args):
            if "FROM code_index_files" in sql:
                self.query_count += 1
            return self.c.execute(sql, *args)

        def commit(self):
            self.c.commit()

        def close(self):
            self.c.close()

    wrapper = ConnectionWrapper(conn)
    try:
        report = run_retrieval_doctor(wrapper)  # type: ignore[arg-type]
        assert len(report["orphaned_file_references"]) == 0
        # Should call database for files list exactly once because of files_cache
        assert wrapper.query_count == 1
    finally:
        conn.close()


def test_merge_readiness_scoring_priorities(tmp_path):
    """Verify PR readiness score calculation under various thread states."""
    conn = DatabaseManager(tmp_path / "oss_context.db").initialize()
    now = datetime.now(UTC)
    try:
        # Seed Repo
        conn.execute(
            (
                "INSERT INTO repos(id, github_id, owner, name, default_branch, last_synced_at) "
                "VALUES(1, 10, 'acme', 'widgets', 'main', ?)"
            ),
            (now.isoformat(),),
        )
        # Seed PR
        conn.execute(
            """
            INSERT INTO prs(
                id, github_id, repo_id, number, title, state, author,
                created_at, updated_at, body, base_branch, head_branch, merge_commit_sha
            ) VALUES(
                1, 101, 1, 42, 'Auth', 'open', 'alice',
                ?, ?, 'body', 'main', 'feature/auth', NULL
            )
            """,
            ((now - timedelta(days=1)).isoformat(), now.isoformat()),
        )
        # Seed an unresolved thread (waiting on author, non-blocking)
        conn.execute(
            """
            INSERT INTO review_threads(
                id, github_thread_id, pr_id, file_path, line_number,
                thread_state, resolved_by, resolved_at, created_at, updated_at
            ) VALUES(1, 'thread-1', 1, 'service.py', 6, 'active', NULL, NULL, ?, ?)
            """,
            ((now - timedelta(hours=20)).isoformat(), now.isoformat()),
        )
        conn.execute(
            """
            INSERT INTO review_comments(
                id, thread_id, github_comment_id, author, body, created_at, updated_at,
                reaction_count, is_suggestion, suggestion_applied,
                extracted_decision, decision_confidence
            ) VALUES(
                1, 1, 5001, 'bob', 'Please fix this.',
                ?, ?, 0, 0, 0, 'COMMENT', 0.95
            )
            """,
            ((now - timedelta(hours=20)).isoformat(), (now - timedelta(hours=20)).isoformat()),
        )
        # Seed analysis in llm_cache
        conn.execute(
            """
            INSERT INTO llm_cache(
                comment_id, provider, model, input_hash,
                decision_type, summary, confidence, analyzed_at
            ) VALUES(
                1, 'heuristic', 'heuristic-v1', 'hash-1',
                'COMMENT', 'Fix this.', 0.95, ?
            )
            """,
            (now.isoformat(),),
        )
        conn.commit()

        # Score with 1 non-blocking thread waiting on author
        payload = get_merge_readiness_payload(conn, repo="acme/widgets", pr_number=42)
        assert payload["blocking_threads"] == 0
        assert payload["unresolved_threads"] == 1
        assert payload["readiness_label"] == "needs author action"

    finally:
        conn.close()


def test_github_api_error_properties():
    """Verify GitHubApiError retains HTTP status, response, operation, and repo slug."""
    from oss_context.github import GitHubApiError
    exc = GitHubApiError(
        "failed",
        http_status=403,
        response_text='{"message": "rate limit"}',
        operation="fetch_review_threads",
        repo="lima-vm/lima",
    )
    assert exc.http_status == 403
    assert exc.response_text == '{"message": "rate limit"}'
    assert exc.operation == "fetch_review_threads"
    assert exc.repo == "lima-vm/lima"
    assert str(exc) == "failed"


def test_cli_sync_since_duration_parsing(tmp_path):
    """Verify cli sync --since duration validation."""
    from typer.testing import CliRunner

    from oss_context.cli import app

    runner = CliRunner()

    result = runner.invoke(
        app,
        ["sync", "lima-vm/lima", "--since", "invalid", "--db-path", str(tmp_path / "test.db")],
    )
    assert result.exit_code != 0
    assert "Invalid since duration format" in result.output

    result2 = runner.invoke(
        app,
        ["sync", "lima-vm/lima", "--since", "5m", "--db-path", str(tmp_path / "test.db")],
    )
    assert result2.exit_code != 0
    assert "Invalid since duration format" in result2.output


def test_cli_sync_since_success(tmp_path):
    """Verify that a successful since option parses to a UTC datetime and invokes sync."""
    from datetime import UTC, datetime, timedelta
    from unittest.mock import patch

    from typer.testing import CliRunner

    from oss_context.cli import app

    runner = CliRunner()

    with patch("oss_context.cli.sync_repository") as mock_sync:
        from oss_context.models import SyncReport
        mock_sync.return_value = SyncReport(repo="lima-vm/lima", started_at=datetime.now(UTC))

        result = runner.invoke(
            app,
            ["sync", "lima-vm/lima", "--since", "90d", "--db-path", str(tmp_path / "test.db")],
        )
        assert result.exit_code == 0
        mock_sync.assert_called_once()
        args, kwargs = mock_sync.call_args
        since_val = kwargs.get("since_override")
        assert since_val is not None
        assert since_val.tzinfo == UTC
        now = datetime.now(UTC)
        expected_delta = timedelta(days=90)
        delta = now - since_val
        assert abs(delta.total_seconds() - expected_delta.total_seconds()) < 60


def test_resolve_branch_pr_with_fork_upstream(tmp_path):
    """Verify that we resolve synced upstream repository when local remote is a fork."""
    from oss_context.branch_context import resolve_branch_pr
    from oss_context.db import DatabaseManager

    connection = DatabaseManager(tmp_path / "test.db").initialize()
    try:
        connection.execute(
            "INSERT INTO repos(github_id, owner, name) VALUES(1, 'lima-vm', 'lima')"
        )
        connection.commit()

        def runner(args, cwd=None, allow_failure=False):
            if args == ["git", "rev-parse", "--show-toplevel"]:
                return str(tmp_path)
            if args == ["git", "branch", "--show-current"]:
                return "image-variant"
            if args == ["git", "remote"]:
                return "origin\nupstream\n"
            if args == ["git", "remote", "get-url", "origin"]:
                return "https://github.com/Nachiket-Roy/lima.git"
            if args == ["git", "remote", "get-url", "upstream"]:
                return "https://github.com/lima-vm/lima.git"
            raise AssertionError(f"Unexpected: {args}")

        connection.execute(
            """
            INSERT INTO branch_links(repo_slug, branch_name, pr_number, linked_at)
            VALUES('lima-vm/lima', 'image-variant', 5159, '2026-06-28 12:00:00')
            """
        )
        connection.commit()

        resolved = resolve_branch_pr(connection, runner=runner)
        assert resolved["repo"] == "lima-vm/lima"
        assert resolved["pr_number"] == 5159
        assert resolved["branch"] == "image-variant"
        assert resolved["source"] == "manual_link"

    finally:
        connection.close()
