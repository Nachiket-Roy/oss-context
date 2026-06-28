"""Tests for merge-readiness and review follow-up guidance.

This file verifies the higher-level assistant summary built from synced PR
health, unresolved-thread state, and extracted cross-reference data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from oss_context.db import DatabaseManager
from oss_context.review_assistant import get_merge_readiness_payload


def test_merge_readiness_payload_summarizes_blockers_and_references(tmp_path):
    connection = DatabaseManager(tmp_path / "oss_context.db").initialize()
    try:
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
                1, 101, 1, 42, 'Auth hardening', 'open', 'alice',
                ?, ?, 'See issue 44', 'main', 'feature/auth', NULL
            )
            """,
            ((now - timedelta(days=3)).isoformat(), now.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO review_threads(
                id, github_thread_id, pr_id, file_path, line_number,
                thread_state, resolved_by, resolved_at, created_at, updated_at
            ) VALUES(1, 'thread-1', 1, 'service.py', 12, 'active', NULL, NULL, ?, ?)
            """,
            ((now - timedelta(days=3)).isoformat(), (now - timedelta(days=3)).isoformat()),
        )
        connection.execute(
            """
            INSERT INTO review_comments(
                id, thread_id, github_comment_id, author, body, created_at, updated_at,
                reaction_count, is_suggestion, suggestion_applied,
                extracted_decision, decision_confidence
            ) VALUES(
                1, 1, 5001, 'bob', 'Please change this before merge.',
                ?, ?, 0, 0, 0, 'REQUEST_CHANGES', 0.95
            )
            """,
            ((now - timedelta(days=3)).isoformat(), (now - timedelta(days=3)).isoformat()),
        )
        connection.execute(
            """
            INSERT INTO llm_cache(
                comment_id, provider, model, input_hash,
                decision_type, summary, confidence, analyzed_at
            ) VALUES(
                1, 'heuristic', 'heuristic-v1', 'hash-1',
                'REQUEST_CHANGES', 'Change this before merge.', 0.95, ?
            )
            """,
            (now.isoformat(),),
        )
        connection.execute(
            """
            INSERT INTO extracted_references(
                id, source_kind, source_id, repo_id, reference_kind, raw_text, url,
                target_repo, target_number, target_sha
            ) VALUES(1, 'pr', 1, 1, 'issue', 'issue 44', NULL, 'acme/widgets', 44, NULL)
            """
        )
        connection.commit()

        payload = get_merge_readiness_payload(
            connection,
            repo="acme/widgets",
            pr_number=42,
            stale_days=2,
        )
        assert payload["blocking_threads"] == 1
        assert payload["waiting_on_author_threads"] == 1
        assert payload["readiness_label"] == "needs author action"
        assert payload["stale_threads"] == 1
        assert any("service.py" in item for item in payload["recommended_actions"])
        assert payload["linked_references"] == ["acme/widgets#44"]
    finally:
        connection.close()
