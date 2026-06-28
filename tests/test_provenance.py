"""Provenance tests to verify complete lineage tracking of code symbols and comments."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime

from oss_context.code_index import (
    get_combined_file_context,
    index_codebase,
    search_symbols,
)
from oss_context.db import DatabaseManager

SOURCE_CODE = """def hello():
    pass
"""


def test_symbol_provenance_lineage(tmp_path):
    """Verify code symbols are tracked back to their exact snapshot, repo, and branch."""
    db_path = tmp_path / "oss_context.db"
    repo_root = tmp_path / "workspace"
    repo_root.mkdir()

    # Initialize git repo to test git integration
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_root), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo_root), check=True)
    subprocess.run(["git", "config", "user.email", "t@ex.com"], cwd=str(repo_root), check=True)

    # Create dummy python file
    (repo_root / "hello.py").write_text(SOURCE_CODE, encoding="utf-8")
    subprocess.run(["git", "add", "hello.py"], cwd=str(repo_root), check=True)
    subprocess.run(["git", "commit", "-m", "commit123"], cwd=str(repo_root), check=True)

    commit_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    conn = DatabaseManager(db_path).initialize()
    now = datetime.now(UTC)
    try:
        # Seed Repo in SQLite
        conn.execute(
            (
                "INSERT INTO repos(id, github_id, owner, name, default_branch, last_synced_at) "
                "VALUES(1, 10, 'acme', 'widgets', 'main', ?)"
            ),
            (now.isoformat(),),
        )
        conn.commit()

        # Index codebase on main branch
        index_codebase(
            conn,
            cwd=repo_root,
            repo="acme/widgets",
        )

        # Retrieve indexed snapshot details
        snapshot = conn.execute(
            "SELECT * FROM code_index_snapshots WHERE repo_slug = ?",
            ("acme/widgets",),
        ).fetchone()

        assert snapshot is not None
        assert snapshot["git_branch"] == "main"
        assert snapshot["git_commit"] == commit_sha

        # Search for symbols and verify lineage/provenance links
        symbols = search_symbols(conn, query="hello", repo="acme/widgets", branch="main")
        assert len(symbols) == 1
        symbol = symbols[0]

        # Verify that symbol attributes accurately match source code file paths
        assert symbol["file_path"] == "hello.py"
        assert symbol["qualified_name"] == "hello"

        # Verify symbols table has matching snapshot foreign keys
        db_symbol = conn.execute(
            "SELECT * FROM code_symbols WHERE qualified_name = 'hello'"
        ).fetchone()
        assert db_symbol is not None

        db_file = conn.execute(
            "SELECT * FROM code_index_files WHERE id = ?",
            (db_symbol["file_id"],),
        ).fetchone()
        assert db_file is not None
        assert db_file["snapshot_id"] == snapshot["id"]
        assert db_file["file_path"] == "hello.py"

        # Verify file context lineage
        context = get_combined_file_context(
            conn,
            file_path="hello.py",
            repo="acme/widgets",
            branch="main",
        )
        assert context["file_path"] == "hello.py"
        assert any(row["qualified_name"] == "hello" for row in context["symbols"])
    finally:
        conn.close()
