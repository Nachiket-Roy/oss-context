from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from oss_context.models import PRHealthSummary


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(UTC)


def _short_text(value: str | None, limit: int = 88) -> str:
    if not value:
        return ""
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def _base_thread_query() -> str:
    return """
    SELECT
        t.id AS thread_id,
        t.file_path,
        t.line_number,
        t.thread_state,
        t.updated_at AS thread_updated_at,
        p.number AS pr_number,
        p.title AS pr_title,
        p.state AS pr_state,
        p.author AS pr_author,
        p.updated_at AS pr_updated_at,
        r.owner,
        r.name,
        (
            SELECT rc.author
            FROM review_comments rc
            WHERE rc.thread_id = t.id
            ORDER BY rc.created_at ASC, rc.id ASC
            LIMIT 1
        ) AS reviewer,
        (
            SELECT rc.author
            FROM review_comments rc
            WHERE rc.thread_id = t.id
            ORDER BY rc.created_at DESC, rc.id DESC
            LIMIT 1
        ) AS last_author,
        (
            SELECT rc.body
            FROM review_comments rc
            WHERE rc.thread_id = t.id
            ORDER BY rc.created_at DESC, rc.id DESC
            LIMIT 1
        ) AS last_body,
        (
            SELECT rc.created_at
            FROM review_comments rc
            WHERE rc.thread_id = t.id
            ORDER BY rc.created_at DESC, rc.id DESC
            LIMIT 1
        ) AS last_comment_at,
        (
            SELECT cache.decision_type
            FROM review_comments rc
            LEFT JOIN llm_cache cache ON cache.comment_id = rc.id
            WHERE rc.thread_id = t.id AND cache.decision_type IS NOT NULL
            ORDER BY cache.confidence DESC, rc.created_at DESC, rc.id DESC
            LIMIT 1
        ) AS decision_type,
        (
            SELECT cache.summary
            FROM review_comments rc
            LEFT JOIN llm_cache cache ON cache.comment_id = rc.id
            WHERE rc.thread_id = t.id AND cache.summary IS NOT NULL
            ORDER BY cache.confidence DESC, rc.created_at DESC, rc.id DESC
            LIMIT 1
        ) AS decision_summary,
        (
            SELECT cache.confidence
            FROM review_comments rc
            LEFT JOIN llm_cache cache ON cache.comment_id = rc.id
            WHERE rc.thread_id = t.id AND cache.confidence IS NOT NULL
            ORDER BY cache.confidence DESC, rc.created_at DESC, rc.id DESC
            LIMIT 1
        ) AS decision_confidence
    FROM review_threads t
    JOIN prs p ON p.id = t.pr_id
    JOIN repos r ON r.id = p.repo_id
    """


def _annotate_thread(row: sqlite3.Row) -> dict[str, Any]:
    repo = f"{row['owner']}/{row['name']}"
    reviewer = row["reviewer"] or "unknown"
    last_author = row["last_author"]
    pr_author = row["pr_author"]
    waiting_on = pr_author if last_author != pr_author else reviewer
    state = "PENDING_AUTHOR" if waiting_on == pr_author else "WAITING_ON_REVIEWER"
    decision_type = row["decision_type"] or "QUESTION"
    summary = _short_text(row["decision_summary"] or row["last_body"])
    last_activity = _parse_dt(row["last_comment_at"] or row["thread_updated_at"])
    age_days = (datetime.now(UTC) - last_activity).days if last_activity else 0
    blocking = decision_type == "REQUEST_CHANGES"
    return {
        "repo": repo,
        "thread_id": row["thread_id"],
        "pr_number": row["pr_number"],
        "pr_title": row["pr_title"],
        "pr_state": row["pr_state"],
        "pr_author": pr_author,
        "file_path": row["file_path"] or "—",
        "line_number": row["line_number"],
        "reviewer": reviewer,
        "decision_type": decision_type,
        "decision_confidence": row["decision_confidence"] or 0.0,
        "summary": summary,
        "last_author": last_author,
        "waiting_on": waiting_on,
        "reviewer_state": state,
        "blocking": blocking,
        "age_days": age_days,
    }


def list_unresolved_threads(
    connection: sqlite3.Connection,
    *,
    repo: str | None = None,
    author: str | None = None,
    label: str | None = None,
    stale_days: int | None = None,
) -> list[dict[str, Any]]:
    query = _base_thread_query() + " WHERE t.thread_state = 'active'"
    params: list[object] = []

    if repo:
        owner, name = repo.split("/", maxsplit=1)
        query += " AND r.owner = ? AND r.name = ?"
        params.extend([owner, name])
    if label:
        query += " AND EXISTS (SELECT 1 FROM pr_labels l WHERE l.pr_id = p.id AND l.label = ?)"
        params.append(label)

    query += " ORDER BY p.updated_at DESC, t.updated_at DESC, t.id DESC"
    rows = connection.execute(query, params).fetchall()
    threads = [_annotate_thread(row) for row in rows]

    if author:
        normalized_author = author.lstrip("@")
        threads = [thread for thread in threads if thread["reviewer"] == normalized_author]
    if stale_days is not None:
        threads = [thread for thread in threads if thread["age_days"] >= stale_days]
    return threads


def get_pr_decisions(
    connection: sqlite3.Connection,
    *,
    repo: str,
    pr_number: int,
) -> list[dict[str, Any]]:
    owner, name = repo.split("/", maxsplit=1)
    rows = connection.execute(
        """
        SELECT
            c.id AS comment_id,
            c.author,
            c.body,
            c.created_at,
            c.extracted_decision,
            c.decision_confidence,
            t.file_path,
            t.line_number,
            cache.summary
        FROM review_comments c
        JOIN review_threads t ON t.id = c.thread_id
        JOIN prs p ON p.id = t.pr_id
        JOIN repos r ON r.id = p.repo_id
        LEFT JOIN llm_cache cache ON cache.comment_id = c.id
        WHERE r.owner = ? AND r.name = ? AND p.number = ? AND c.extracted_decision IS NOT NULL
        ORDER BY c.created_at ASC, c.id ASC
        """,
        (owner, name, pr_number),
    ).fetchall()

    return [
        {
            "comment_id": row["comment_id"],
            "author": row["author"] or "unknown",
            "decision_type": row["extracted_decision"],
            "confidence": row["decision_confidence"] or 0.0,
            "summary": _short_text(row["summary"] or row["body"]),
            "file_path": row["file_path"] or "—",
            "line_number": row["line_number"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def get_pr_health(
    connection: sqlite3.Connection,
    *,
    repo: str,
    pr_number: int,
) -> PRHealthSummary:
    owner, name = repo.split("/", maxsplit=1)
    pr_row = connection.execute(
        """
        SELECT p.title, p.state, p.author, p.updated_at
        FROM prs p
        JOIN repos r ON r.id = p.repo_id
        WHERE r.owner = ? AND r.name = ? AND p.number = ?
        """,
        (owner, name, pr_number),
    ).fetchone()
    if not pr_row:
        raise ValueError(f"PR #{pr_number} not found for {repo}")

    unresolved = [
        thread
        for thread in list_unresolved_threads(connection, repo=repo)
        if thread["pr_number"] == pr_number
    ]
    decisions = get_pr_decisions(connection, repo=repo, pr_number=pr_number)

    approvals = sum(1 for row in decisions if row["decision_type"] == "APPROVE")
    questions = sum(1 for row in decisions if row["decision_type"] == "QUESTION")
    suggestions = sum(1 for row in decisions if row["decision_type"] == "SUGGESTION")
    acknowledgments = sum(1 for row in decisions if row["decision_type"] == "ACKNOWLEDGMENT")
    blocking_threads = sum(1 for row in unresolved if row["blocking"])
    stale_penalty = sum(1 for row in unresolved if row["age_days"] >= 2)

    health_score = 100
    health_score -= blocking_threads * 35
    health_score -= len(unresolved) * 10
    health_score -= stale_penalty * 5
    health_score += min(approvals * 3, 10)
    health_score = max(0, min(100, health_score))

    reviewer_states = [
        {
            "reviewer": row["reviewer"],
            "decision": row["decision_type"],
            "state": row["reviewer_state"],
            "waiting_on": row["waiting_on"],
            "file_path": row["file_path"],
            "summary": row["summary"],
        }
        for row in unresolved
    ]

    return PRHealthSummary(
        repo=repo,
        pr_number=pr_number,
        title=pr_row["title"],
        state=pr_row["state"],
        author=pr_row["author"],
        health_score=health_score,
        unresolved_threads=len(unresolved),
        blocking_threads=blocking_threads,
        approvals=approvals,
        questions=questions,
        suggestions=suggestions,
        acknowledgments=acknowledgments,
        updated_at=_parse_dt(pr_row["updated_at"]),
        reviewer_states=reviewer_states,
    )


def get_reviewer_status(
    connection: sqlite3.Connection,
    *,
    repo: str,
    reviewer: str,
) -> dict[str, Any]:
    threads = list_unresolved_threads(connection, repo=repo, author=reviewer)
    blocking = [thread for thread in threads if thread["blocking"]]
    return {
        "repo": repo,
        "reviewer": reviewer.lstrip("@"),
        "unresolved_threads": len(threads),
        "blocking_threads": len(blocking),
        "threads": threads,
    }
