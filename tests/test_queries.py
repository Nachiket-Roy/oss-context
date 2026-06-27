"""Tests for SQLite-backed query behavior.

This file seeds a local temporary database and verifies unresolved-thread,
health-score, and latest-comment decision behavior across representative PR
review scenarios.
"""

from datetime import UTC, datetime, timedelta

from oss_context.db import DatabaseManager
from oss_context.queries import (
    get_dashboard_summary,
    get_pr_context_payload,
    get_pr_health,
    get_reviewer_status,
    list_tracked_repos,
    list_unresolved_threads,
)


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


def test_dashboard_summary_and_reviewer_status(tmp_path):
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
        (
            "INSERT INTO repos(id, github_id, owner, name, default_branch, last_synced_at) "
            "VALUES(2, 11, 'acme', 'gadgets', 'main', ?)"
        ),
        (now.isoformat(),),
    )
    _ = connection.execute(
        """
        INSERT INTO prs(
            id, github_id, repo_id, number, title, state, author,
            created_at, updated_at, body, base_branch, head_branch, merge_commit_sha
        )
        VALUES(1, 101, 1, 42, 'Auth', 'open', 'alice', ?, ?, 'body', 'main', 'feature/auth', NULL)
        """,
        ((now - timedelta(days=3)).isoformat(), now.isoformat()),
    )
    _ = connection.execute(
        """
        INSERT INTO prs(
            id, github_id, repo_id, number, title, state, author,
            created_at, updated_at, body, base_branch, head_branch, merge_commit_sha
        )
        VALUES(
            2, 102, 2, 7, 'Payments', 'open', 'eve',
            ?, ?, 'body', 'main', 'feature/payments', NULL
        )
        """,
        ((now - timedelta(days=1)).isoformat(), now.isoformat()),
    )
    _ = connection.execute(
        """
        INSERT INTO review_threads(
            id, github_thread_id, pr_id, file_path, line_number,
            thread_state, resolved_by, resolved_at, created_at, updated_at
        )
        VALUES(1, 'thread-1', 1, 'auth.py', 42, 'active', NULL, NULL, ?, ?)
        """,
        ((now - timedelta(days=3)).isoformat(), now.isoformat()),
    )
    _ = connection.execute(
        """
        INSERT INTO review_threads(
            id, github_thread_id, pr_id, file_path, line_number,
            thread_state, resolved_by, resolved_at, created_at, updated_at
        )
        VALUES(2, 'thread-2', 2, 'payments.py', 11, 'active', NULL, NULL, ?, ?)
        """,
        ((now - timedelta(days=1)).isoformat(), now.isoformat()),
    )
    _ = connection.execute(
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
    _ = connection.execute(
        """
        INSERT INTO review_comments(
            id, thread_id, github_comment_id, author, body, created_at, updated_at,
            reaction_count, is_suggestion, suggestion_applied,
            extracted_decision, decision_confidence
        ) VALUES(
            2, 2, 5002, 'carol', 'Can you add one more test?',
            ?, ?, 0, 0, 0, 'QUESTION', 0.66
        )
        """,
        ((now - timedelta(days=1)).isoformat(), (now - timedelta(days=1)).isoformat()),
    )
    _ = connection.execute(
        """
        INSERT INTO review_comments(
            id, thread_id, github_comment_id, author, body, created_at, updated_at,
            reaction_count, is_suggestion, suggestion_applied,
            extracted_decision, decision_confidence
        ) VALUES(
            3, 2, 5003, 'eve', 'Done, updated.',
            ?, ?, 0, 0, 0, 'ACKNOWLEDGMENT', 0.41
        )
        """,
        ((now - timedelta(hours=2)).isoformat(), (now - timedelta(hours=2)).isoformat()),
    )
    _ = connection.execute(
        """
        INSERT INTO llm_cache(
            comment_id, provider, model, input_hash,
            decision_type, summary, confidence, analyzed_at
        ) VALUES(
            1, 'heuristic', 'heuristic-v1', 'hash-1',
            'REQUEST_CHANGES', 'Blocking fix', 0.95, ?
        )
        """,
        (now.isoformat(),),
    )
    _ = connection.execute(
        """
        INSERT INTO llm_cache(
            comment_id, provider, model, input_hash,
            decision_type, summary, confidence, analyzed_at
        ) VALUES(
            2, 'heuristic', 'heuristic-v1', 'hash-2',
            'QUESTION', 'Need one more test', 0.66, ?
        )
        """,
        (now.isoformat(),),
    )
    _ = connection.execute(
        """
        INSERT INTO llm_cache(
            comment_id, provider, model, input_hash,
            decision_type, summary, confidence, analyzed_at
        ) VALUES(
            3, 'heuristic', 'heuristic-v1', 'hash-3',
            'ACKNOWLEDGMENT', 'Done, updated.', 0.41, ?
        )
        """,
        (now.isoformat(),),
    )
    _ = connection.execute(
        "INSERT INTO pr_labels(pr_id, label, added_at) VALUES(1, 'security', ?)",
        (now.isoformat(),),
    )
    connection.commit()

    repos = list_tracked_repos(connection)
    assert len(repos) == 2

    dashboard = get_dashboard_summary(connection, stale_days=2)
    assert dashboard["repos_tracked"] == 2
    assert dashboard["open_prs"] == 2
    assert dashboard["unresolved_threads"] == 2
    assert dashboard["blocking_threads"] == 1
    assert dashboard["stale_threads"] == 1

    reviewer_status = get_reviewer_status(connection, reviewer="carol")
    assert reviewer_status["unresolved_threads"] == 1
    assert reviewer_status["pending_threads"] == 1
    assert reviewer_status["waiting_on_author_threads"] == 0

    pr_context = get_pr_context_payload(connection, repo="acme/widgets", pr_number=42)
    assert pr_context["repo_status"]["repo"] == "acme/widgets"
    assert pr_context["labels"] == ["security"]
    assert pr_context["health"]["blocking_threads"] == 1

    connection.close()
