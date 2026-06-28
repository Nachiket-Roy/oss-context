"""Tests for local code indexing and file-context retrieval.

This file exercises the SQLite-backed Python code index, including symbol search,
caller/callee lookups, impacted-file discovery, historical review context, and
incremental snapshot reuse between indexing runs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from oss_context.code_index import (
    get_combined_file_context,
    get_impacted_files,
    get_symbol_callees,
    get_symbol_callers,
    index_codebase,
    search_symbols,
)
from oss_context.db import DatabaseManager

SERVICE_SOURCE = """class AuthService:
    def verify(self, token: str) -> str:
        return check_token(token)


def check_token(token: str) -> str:
    return normalize(token)


def normalize(token: str) -> str:
    return token.strip()
"""

HANDLER_SOURCE = """from service import check_token


def handle() -> str:
    return check_token(' value ')
"""

UPDATED_SERVICE_SOURCE = """class AuthService:
    def verify(self, token: str) -> str:
        return resolve_token(token)


def resolve_token(token: str) -> str:
    return normalize(token)


def normalize(token: str) -> str:
    return token.strip()
"""

UPDATED_HANDLER_SOURCE = """from service import resolve_token


def handle() -> str:
    return resolve_token(' value ')


def handle_again() -> str:
    return resolve_token(' again ')
"""


def _seed_review_fixture(connection) -> None:
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
        ) VALUES(
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
        ) VALUES(1, 'thread-1', 1, 'service.py', 6, 'active', NULL, NULL, ?, ?)
        """,
        ((now - timedelta(hours=20)).isoformat(), now.isoformat()),
    )
    connection.execute(
        """
        INSERT INTO review_comments(
            id, thread_id, github_comment_id, author, body, created_at, updated_at,
            reaction_count, is_suggestion, suggestion_applied,
            extracted_decision, decision_confidence
        ) VALUES(
            1, 1, 5001, 'bob', 'Please update check_token before merge.',
            ?, ?, 0, 0, 0, 'REQUEST_CHANGES', 0.91
        )
        """,
        ((now - timedelta(hours=20)).isoformat(), (now - timedelta(hours=20)).isoformat()),
    )
    connection.execute(
        """
        INSERT INTO llm_cache(
            comment_id, provider, model, input_hash,
            decision_type, summary, confidence, analyzed_at
        ) VALUES(
            1, 'heuristic', 'heuristic-v1', 'hash-1',
            'REQUEST_CHANGES', 'Update check_token before merge.', 0.91, ?
        )
        """,
        (now.isoformat(),),
    )
    connection.commit()


def test_code_index_search_context_and_incremental_reuse(tmp_path):
    connection = DatabaseManager(tmp_path / "oss_context.db").initialize()
    try:
        repo_root = tmp_path / "workspace"
        repo_root.mkdir()
        (repo_root / "service.py").write_text(SERVICE_SOURCE, encoding="utf-8")
        (repo_root / "handlers.py").write_text(HANDLER_SOURCE, encoding="utf-8")
        _seed_review_fixture(connection)

        first_report = index_codebase(connection, cwd=repo_root, repo="acme/widgets")
        assert first_report["files_indexed"] == 2
        assert first_report["files_parsed"] == 2
        assert first_report["files_reused"] == 0

        symbols = search_symbols(connection, query="check_token", repo="acme/widgets")
        assert any(row["qualified_name"] == "check_token" for row in symbols)

        callers = get_symbol_callers(connection, symbol="check_token", repo="acme/widgets")
        assert any(row["caller"] == "AuthService.verify" for row in callers)
        assert any(row["caller"] == "handle" for row in callers)

        callees = get_symbol_callees(connection, symbol="check_token", repo="acme/widgets")
        assert any(row["callee"] == "normalize" for row in callees)

        impacted = get_impacted_files(connection, symbol="check_token", repo="acme/widgets")
        assert {row["file_path"] for row in impacted} == {"handlers.py", "service.py"}

        context = get_combined_file_context(
            connection,
            file_path="service.py",
            repo="acme/widgets",
        )
        assert context["file_path"] == "service.py"
        assert any(row["qualified_name"] == "AuthService.verify" for row in context["symbols"])
        assert any(row["caller"] == "handle" for row in context["inbound_calls"])
        assert context["review_history"][0]["pr_number"] == 42

        (repo_root / "service.py").write_text(UPDATED_SERVICE_SOURCE, encoding="utf-8")
        (repo_root / "handlers.py").write_text(UPDATED_HANDLER_SOURCE, encoding="utf-8")
        second_report = index_codebase(connection, cwd=repo_root, repo="acme/widgets")
        assert second_report["files_indexed"] == 2
        assert second_report["files_parsed"] == 2
        assert second_report["files_reused"] == 0

        # Third run: nothing changes -> should reuse snapshot
        third_report = index_codebase(connection, cwd=repo_root, repo="acme/widgets")
        assert third_report["reused_snapshot"] is True
        assert third_report["files_indexed"] == 2
        assert third_report["files_parsed"] == 0
        assert third_report["files_reused"] == 2

        # Fourth run: only service.py changes -> should reuse handlers.py
        (repo_root / "service.py").write_text(UPDATED_SERVICE_SOURCE + "\n# dummy change", encoding="utf-8")
        fourth_report = index_codebase(connection, cwd=repo_root, repo="acme/widgets")
        assert fourth_report["reused_snapshot"] is False
        assert fourth_report["files_indexed"] == 2
        assert fourth_report["files_parsed"] == 1
        assert fourth_report["files_reused"] == 1

        renamed = search_symbols(connection, query="resolve_token", repo="acme/widgets")
        assert any(row["qualified_name"] == "resolve_token" for row in renamed)
        old = search_symbols(connection, query="check_token", repo="acme/widgets")
        assert old == []
    finally:
        connection.close()
