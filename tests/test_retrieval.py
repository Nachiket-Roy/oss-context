"""Tests for retrieval provenance diagnostics.

This file verifies the doctor-style retrieval checks that flag stale branch links,
missing code indexes, outdated snapshots, and orphaned file references.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from oss_context.db import DatabaseManager
from oss_context.retrieval import run_retrieval_doctor


def test_retrieval_doctor_reports_stale_links_missing_indexes_and_orphans(tmp_path):
    connection = DatabaseManager(tmp_path / "oss_context.db").initialize()
    try:
        now = datetime.now(UTC)
        synced_at = now.isoformat()
        old_indexed_at = (now - timedelta(days=2)).isoformat()
        connection.execute(
            (
                "INSERT INTO repos(id, github_id, owner, name, default_branch, last_synced_at) "
                "VALUES(1, 10, 'acme', 'widgets', 'main', ?)"
            ),
            (synced_at,),
        )
        connection.execute(
            (
                "INSERT INTO repos(id, github_id, owner, name, default_branch, last_synced_at) "
                "VALUES(2, 11, 'acme', 'gadgets', 'main', ?)"
            ),
            (synced_at,),
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
            ((now - timedelta(days=1)).isoformat(), synced_at),
        )
        connection.execute(
            """
            INSERT INTO review_threads(
                id, github_thread_id, pr_id, file_path, line_number,
                thread_state, resolved_by, resolved_at, created_at, updated_at
            ) VALUES(1, 'thread-1', 1, 'moved/auth.py', 10, 'active', NULL, NULL, ?, ?)
            """,
            ((now - timedelta(days=1)).isoformat(), synced_at),
        )
        connection.execute(
            """
            INSERT INTO branch_links(repo_slug, branch_name, pr_number, linked_at)
            VALUES('acme/widgets', 'feature/missing', 99, ?)
            """,
            (synced_at,),
        )
        connection.execute(
            """
            INSERT INTO code_index_snapshots(
                repo_slug, repo_root, git_branch, git_commit, indexed_at
            ) VALUES('acme/widgets', '/tmp/widgets', 'main', 'abc123', ?)
            """,
            (old_indexed_at,),
        )
        snapshot_id = connection.execute(
            "SELECT MAX(id) AS id FROM code_index_snapshots WHERE repo_slug = 'acme/widgets'"
        ).fetchone()["id"]
        connection.execute(
            """
            INSERT INTO code_index_files(snapshot_id, file_path, content_hash, language)
            VALUES(?, 'src/auth.py', 'hash-1', 'python')
            """,
            (snapshot_id,),
        )
        connection.commit()

        report = run_retrieval_doctor(connection)
        assert report["healthy"] is False
        assert report["stale_branch_links"] == [
            {"repo": "acme/widgets", "branch": "feature/missing", "pr_number": 99}
        ]
        assert report["missing_code_indexes"] == [
            {"repo": "acme/gadgets", "last_synced_at": synced_at}
        ]
        assert report["outdated_code_indexes"] == [
            {
                "repo": "acme/widgets",
                "indexed_at": old_indexed_at,
                "last_synced_at": synced_at,
            }
        ]
        assert report["orphaned_file_references"] == [
            {
                "repo": "acme/widgets",
                "pr_number": 42,
                "thread_id": "thread-1",
                "file_path": "moved/auth.py",
            }
        ]
    finally:
        connection.close()
