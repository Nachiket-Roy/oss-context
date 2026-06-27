"""Tests for SQLite-backed query behavior.

This file seeds temporary databases and verifies unresolved-thread views, latest
classified-comment behavior, issue context payloads, references, and filtered
cross-repo dashboard summaries.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from oss_context.db import DatabaseManager
from oss_context.queries import (
    get_dashboard_summary,
    get_issue_context_payload,
    get_pr_context_payload,
    get_pr_health,
    get_reviewer_status,
    list_repo_issues,
    list_tracked_repos,
    list_unresolved_threads,
    search_issues,
    search_pull_requests,
    search_work_items,
)


def test_health_summary_and_unresolved_threads(tmp_path):
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
    finally:
        connection.close()


def test_unresolved_thread_uses_latest_classified_comment(tmp_path):
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
            ((now - timedelta(days=2)).isoformat(), now.isoformat()),
        )
        connection.execute(
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
        connection.execute(
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
        connection.execute(
            """
            INSERT INTO llm_cache(
                comment_id, provider, model, input_hash,
                decision_type, summary, confidence, analyzed_at
            )
            VALUES(
                1, 'heuristic', 'heuristic-v1', 'hash-1',
                'REQUEST_CHANGES', 'Blocking fix', 0.99, ?
            )
            """,
            ((now - timedelta(minutes=2)).isoformat(),),
        )
        connection.execute(
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
            ((now - timedelta(minutes=1)).isoformat(),),
        )
        connection.commit()

        unresolved = list_unresolved_threads(connection, repo="acme/widgets")
        assert len(unresolved) == 1
        assert unresolved[0]["decision_type"] == "ACKNOWLEDGMENT"
        assert unresolved[0]["blocking"] is False
        assert unresolved[0]["summary"] == "Done, updated."
    finally:
        connection.close()


def _seed_multi_repo_fixture(connection) -> None:
    now = datetime.now(UTC)
    connection.execute(
        (
            "INSERT INTO repos(id, github_id, owner, name, default_branch, last_synced_at) "
            "VALUES(1, 10, 'acme', 'widgets', 'main', ?)"
        ),
        (now.isoformat(),),
    )
    connection.execute(
        (
            "INSERT INTO repos(id, github_id, owner, name, default_branch, last_synced_at) "
            "VALUES(2, 11, 'acme', 'gadgets', 'main', ?)"
        ),
        (now.isoformat(),),
    )
    connection.execute(
        """
        INSERT INTO prs(
            id, github_id, repo_id, number, title, state, author,
            created_at, updated_at, body, base_branch, head_branch, merge_commit_sha
        )
        VALUES(1, 101, 1, 42, 'Auth', 'open', 'alice', ?, ?, 'body', 'main', 'feature/auth', NULL)
        """,
        ((now - timedelta(days=3)).isoformat(), now.isoformat()),
    )
    connection.execute(
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
    connection.execute(
        """
        INSERT INTO issues(
            id, github_id, repo_id, number, title, state, author,
            created_at, updated_at, closed_at, body
        )
        VALUES(1, 201, 1, 44, 'Follow-up auth issue', 'open', 'dave', ?, ?, NULL, 'See PR #42')
        """,
        ((now - timedelta(days=2)).isoformat(), now.isoformat()),
    )
    connection.execute(
        """
        INSERT INTO issues(
            id, github_id, repo_id, number, title, state, author,
            created_at, updated_at, closed_at, body
        )
        VALUES(2, 202, 2, 18, 'Billing issue', 'open', 'frank', ?, ?, NULL, 'Needs triage')
        """,
        ((now - timedelta(days=1)).isoformat(), now.isoformat()),
    )
    connection.execute(
        """
        INSERT INTO review_threads(
            id, github_thread_id, pr_id, file_path, line_number,
            thread_state, resolved_by, resolved_at, created_at, updated_at
        )
        VALUES(1, 'thread-1', 1, 'auth.py', 42, 'active', NULL, NULL, ?, ?)
        """,
        ((now - timedelta(days=3)).isoformat(), now.isoformat()),
    )
    connection.execute(
        """
        INSERT INTO review_threads(
            id, github_thread_id, pr_id, file_path, line_number,
            thread_state, resolved_by, resolved_at, created_at, updated_at
        )
        VALUES(2, 'thread-2', 2, 'payments.py', 11, 'active', NULL, NULL, ?, ?)
        """,
        ((now - timedelta(days=1)).isoformat(), now.isoformat()),
    )
    connection.execute(
        """
        INSERT INTO review_comments(
            id, thread_id, github_comment_id, author, body, created_at, updated_at,
            reaction_count, is_suggestion, suggestion_applied,
            extracted_decision, decision_confidence
        ) VALUES(
            1, 1, 5001, 'bob', 'Please change this before merge. issue 44',
            ?, ?, 0, 0, 0, 'REQUEST_CHANGES', 0.95
        )
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
            2, 2, 5002, 'carol', 'Can you add one more test?',
            ?, ?, 0, 0, 0, 'QUESTION', 0.66
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
            3, 2, 5003, 'eve', 'Done, updated.',
            ?, ?, 0, 0, 0, 'ACKNOWLEDGMENT', 0.41
        )
        """,
        ((now - timedelta(hours=2)).isoformat(), (now - timedelta(hours=2)).isoformat()),
    )
    connection.execute(
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
    connection.execute(
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
    connection.execute(
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
    connection.execute(
        "INSERT INTO pr_labels(pr_id, label, added_at) VALUES(1, 'security', ?)",
        (now.isoformat(),),
    )
    connection.execute(
        "INSERT INTO issue_labels(issue_id, label, added_at) VALUES(1, 'bug', ?)",
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
    connection.execute(
        """
        INSERT INTO extracted_references(
            id, source_kind, source_id, repo_id, reference_kind, raw_text, url,
            target_repo, target_number, target_sha
        ) VALUES(2, 'comment', 1, 1, 'issue', 'issue 44', NULL, 'acme/widgets', 44, NULL)
        """
    )
    connection.execute(
        """
        INSERT INTO extracted_references(
            id, source_kind, source_id, repo_id, reference_kind, raw_text, url,
            target_repo, target_number, target_sha
        ) VALUES(3, 'issue', 1, 1, 'pull_request', 'PR #42', NULL, 'acme/widgets', 42, NULL)
        """
    )
    connection.commit()


def test_issue_listing_and_search_queries(tmp_path):
    connection = DatabaseManager(tmp_path / "oss_context.db").initialize()
    try:
        _seed_multi_repo_fixture(connection)

        issues = list_repo_issues(connection, repo="acme/widgets")
        assert len(issues) == 1
        assert issues[0]["issue_number"] == 44
        assert issues[0]["labels"] == ["bug"]

        pr_text_results = search_pull_requests(connection, text="auth")
        assert any(row["repo"] == "acme/widgets" and row["number"] == 42 for row in pr_text_results)

        pr_reference_results = search_pull_requests(
            connection,
            repo="acme/widgets",
            reference="#44",
        )
        assert [row["number"] for row in pr_reference_results] == [42]

        issue_text_results = search_issues(connection, text="billing")
        assert [row["number"] for row in issue_text_results] == [18]

        issue_reference_results = search_issues(
            connection,
            repo="acme/widgets",
            reference="#42",
        )
        assert [row["number"] for row in issue_reference_results] == [44]

        combined_results = search_work_items(
            connection,
            repo="acme/widgets",
            reference="#42",
        )
        assert combined_results["pull_requests"] == []
        assert [row["number"] for row in combined_results["issues"]] == [44]
    finally:
        connection.close()


def test_dashboard_summary_issue_context_and_filtered_scope(tmp_path):
    connection = DatabaseManager(tmp_path / "oss_context.db").initialize()
    try:
        _seed_multi_repo_fixture(connection)

        repos = list_tracked_repos(connection)
        assert len(repos) == 2
        assert sum(row["open_issues"] for row in repos) == 2

        dashboard = get_dashboard_summary(connection, stale_days=2)
        assert dashboard["repos_tracked"] == 2
        assert dashboard["open_prs"] == 2
        assert dashboard["unresolved_threads"] == 2
        assert dashboard["blocking_threads"] == 1
        assert dashboard["stale_threads"] == 1

        reviewer_dashboard = get_dashboard_summary(connection, reviewer="bob", stale_days=0)
        assert reviewer_dashboard["repos_tracked"] == 1
        assert reviewer_dashboard["open_prs"] == 1
        assert reviewer_dashboard["repo_breakdown"] == [
            {
                "repo": "acme/widgets",
                "open_prs": 1,
                "unresolved_threads": 1,
                "blocking_threads": 1,
                "open_issues": 1,
                "last_synced_at": reviewer_dashboard["repo_breakdown"][0]["last_synced_at"],
            }
        ]

        label_dashboard = get_dashboard_summary(connection, label="security", stale_days=0)
        assert label_dashboard["repos_tracked"] == 1
        assert label_dashboard["open_prs"] == 1
        assert label_dashboard["unresolved_threads"] == 1

        reviewer_status = get_reviewer_status(connection, reviewer="carol")
        assert reviewer_status["unresolved_threads"] == 1
        assert reviewer_status["pending_threads"] == 1
        assert reviewer_status["waiting_on_author_threads"] == 0

        pr_context = get_pr_context_payload(connection, repo="acme/widgets", pr_number=42)
        assert pr_context["repo_status"]["repo"] == "acme/widgets"
        assert pr_context["labels"] == ["security"]
        assert pr_context["health"]["blocking_threads"] == 1
        assert len(pr_context["references"]) == 2

        issue_context = get_issue_context_payload(connection, repo="acme/widgets", issue_number=44)
        assert issue_context["labels"] == ["bug"]
        assert issue_context["references"][0]["reference_kind"] == "pull_request"
        assert issue_context["mentioned_by"][0]["source_label"].startswith("PR #42")
    finally:
        connection.close()
