from datetime import UTC, datetime, timedelta

from oss_context.db import DatabaseManager
from oss_context.queries import get_pr_health, list_unresolved_threads


def test_health_summary_and_unresolved_threads(tmp_path):
    connection = DatabaseManager(tmp_path / "oss_context.db").initialize()
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
            1, 101, 1, 42, 'Add auth middleware', 'open', 'alice',
            ?, ?, 'body', 'main', 'feature/auth', NULL
        )
        """,
        ((now - timedelta(days=2)).isoformat(), now.isoformat()),
    )
    connection.execute(
        """
        INSERT INTO review_threads(
            id, github_thread_id, pr_id, file_path, line_number,
            thread_state, resolved_by, resolved_at, created_at, updated_at
        )
        VALUES(1, 'thread-1', 1, 'auth.py', 42, 'active', NULL, NULL, ?, ?)
        """,
        ((now - timedelta(days=2)).isoformat(), (now - timedelta(days=2)).isoformat()),
    )
    connection.execute(
        """
        INSERT INTO review_comments(
            id, thread_id, github_comment_id, author, body, created_at, updated_at,
            reaction_count, is_suggestion, suggestion_applied,
            extracted_decision, decision_confidence
        ) VALUES(
            1, 1, 5001, 'bob', 'Use constant-time comparison here.',
            ?, ?, 0, 0, 0, 'REQUEST_CHANGES', 0.91
        )
        """,
        ((now - timedelta(days=2)).isoformat(), (now - timedelta(days=2)).isoformat()),
    )
    connection.execute(
        """
        INSERT INTO llm_cache(
            comment_id, provider, model, input_hash,
            decision_type, summary, confidence, analyzed_at
        )
        VALUES(
            1, 'heuristic', 'heuristic-v1', 'hash',
            'REQUEST_CHANGES', 'Use constant-time comparison here.', 0.91, ?
        )
        """,
        (now.isoformat(),),
    )
    connection.commit()

    unresolved = list_unresolved_threads(connection, repo="acme/widgets")
    assert len(unresolved) == 1
    assert unresolved[0]["blocking"] is True
    assert unresolved[0]["waiting_on"] == "alice"

    health = get_pr_health(connection, repo="acme/widgets", pr_number=42)
    assert health.blocking_threads == 1
    assert health.unresolved_threads == 1
    assert health.health_score < 70

    connection.close()


def test_unresolved_thread_uses_latest_classified_comment(tmp_path):
    connection = DatabaseManager(tmp_path / "oss_context.db").initialize()
    now = datetime.now(UTC)
    _ = connection.execute(
        (
            "INSERT INTO repos(id, github_id, owner, name, default_branch, last_synced_at) "
            "VALUES(1, 10, 'acme', 'widgets', 'main', ?)"
        ),
        (now.isoformat(),),
    )
    _ = connection.execute(
        """
        INSERT INTO prs(
            id, github_id, repo_id, number, title, state, author,
            created_at, updated_at, body, base_branch, head_branch, merge_commit_sha
        )
        VALUES(
            1, 101, 1, 42, 'Add auth middleware', 'open', 'alice',
            ?, ?, 'body', 'main', 'feature/auth', NULL
        )
        """,
        ((now - timedelta(days=2)).isoformat(), now.isoformat()),
    )
    _ = connection.execute(
        """
        INSERT INTO review_threads(
            id, github_thread_id, pr_id, file_path, line_number,
            thread_state, resolved_by, resolved_at, created_at, updated_at
        )
        VALUES(1, 'thread-1', 1, 'auth.py', 42, 'active', NULL, NULL, ?, ?)
        """,
        ((now - timedelta(days=2)).isoformat(), now.isoformat()),
    )
    _ = connection.execute(
        """
        INSERT INTO review_comments(
            id, thread_id, github_comment_id, author, body, created_at, updated_at,
            reaction_count, is_suggestion, suggestion_applied,
            extracted_decision, decision_confidence
        ) VALUES(
            1, 1, 5001, 'bob', 'Please change this before merge.',
            ?, ?, 0, 0, 0, 'REQUEST_CHANGES', 0.99
        )
        """,
        ((now - timedelta(days=2)).isoformat(), (now - timedelta(days=2)).isoformat()),
    )
    _ = connection.execute(
        """
        INSERT INTO review_comments(
            id, thread_id, github_comment_id, author, body, created_at, updated_at,
            reaction_count, is_suggestion, suggestion_applied,
            extracted_decision, decision_confidence
        ) VALUES(
            2, 1, 5002, 'alice', 'Done, updated.',
            ?, ?, 0, 0, 0, 'ACKNOWLEDGMENT', 0.40
        )
        """,
        ((now - timedelta(hours=1)).isoformat(), (now - timedelta(hours=1)).isoformat()),
    )
    _ = connection.execute(
        """
        INSERT INTO llm_cache(
            comment_id, provider, model, input_hash,
            decision_type, summary, confidence, analyzed_at
        )
        VALUES(1, 'heuristic', 'heuristic-v1', 'hash-1', 'REQUEST_CHANGES', 'Blocking fix', 0.99, ?)
        """,
        (now.isoformat(),),
    )
    _ = connection.execute(
        """
        INSERT INTO llm_cache(
            comment_id, provider, model, input_hash,
            decision_type, summary, confidence, analyzed_at
        )
        VALUES(
            2, 'heuristic', 'heuristic-v1', 'hash-2',
            'ACKNOWLEDGMENT', 'Done, updated.', 0.40, ?
        )
        """,
        (now.isoformat(),),
    )
    connection.commit()

    unresolved = list_unresolved_threads(connection, repo="acme/widgets")
    assert len(unresolved) == 1
    assert unresolved[0]["decision_type"] == "ACKNOWLEDGMENT"
    assert unresolved[0]["blocking"] is False
    assert unresolved[0]["summary"] == "Done, updated."

    connection.close()
