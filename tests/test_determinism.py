"""Deterministic tests for database indexing and code symbol extraction."""

from __future__ import annotations

from datetime import UTC, datetime

from oss_context.code_index import index_codebase, search_symbols
from oss_context.db import DatabaseManager

DUMMY_SOURCE = """def foo():
    return bar()

def bar():
    return 1
"""


def _seed_repo_fixture(connection) -> None:
    now = datetime.now(UTC)
    connection.execute(
        (
            "INSERT INTO repos(id, github_id, owner, name, default_branch, last_synced_at) "
            "VALUES(1, 10, 'acme', 'widgets', 'main', ?)"
        ),
        (now.isoformat(),),
    )
    connection.commit()


def test_indexing_is_deterministic(tmp_path):
    """Verify reindexing the same codebase produces deterministic, identical outputs."""
    db_path = tmp_path / "oss_context.db"
    repo_root = tmp_path / "workspace"
    repo_root.mkdir()

    # Create dummy source code file
    (repo_root / "app.py").write_text(DUMMY_SOURCE, encoding="utf-8")

    # First index run
    conn1 = DatabaseManager(db_path).initialize()
    try:
        _seed_repo_fixture(conn1)
        report1 = index_codebase(conn1, cwd=repo_root, repo="acme/widgets")
        assert report1["files_indexed"] == 1
        assert report1["files_parsed"] == 1
        assert report1["files_reused"] == 0

        # Retrieve symbols and callers/callees
        symbols1 = search_symbols(conn1, query="foo", repo="acme/widgets")
        assert len(symbols1) == 1
        foo_symbol_1 = symbols1[0]
    finally:
        conn1.close()

    # Second index run with a fresh DB connection on unmodified files
    conn2 = DatabaseManager(db_path).initialize()
    try:
        report2 = index_codebase(conn2, cwd=repo_root, repo="acme/widgets")
        # Ensure snapshot reuse is triggered, resulting in deterministic reuse count
        assert report2["reused_snapshot"] is True
        assert report2["files_indexed"] == 1
        assert report2["files_parsed"] == 0
        assert report2["files_reused"] == 1

        symbols2 = search_symbols(conn2, query="foo", repo="acme/widgets")
        assert len(symbols2) == 1
        foo_symbol_2 = symbols2[0]

        # Verify that symbol definition attributes are identical and deterministic
        assert foo_symbol_1["qualified_name"] == foo_symbol_2["qualified_name"]
        assert foo_symbol_1["file_path"] == foo_symbol_2["file_path"]
        assert foo_symbol_1["line_number"] == foo_symbol_2["line_number"]
    finally:
        conn2.close()
