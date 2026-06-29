"""SQLite query helpers for unresolved state, issues, references, and dashboards.

This module assembles the higher-level read views used by the CLI, MCP server,
and local HTML UI, including unresolved review threads, decision timelines,
linked references, issue context, reviewer status, repository freshness, and
cross-repo dashboard summaries derived from the synced local knowledge graph.
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

from oss_context.models import PRHealthSummary, RepoRef
from oss_context.references import extract_references


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
        latest_cache.decision_type AS decision_type,
        latest_cache.summary AS decision_summary,
        latest_cache.confidence AS decision_confidence
    FROM review_threads t
    JOIN prs p ON p.id = t.pr_id
    JOIN repos r ON r.id = p.repo_id
    LEFT JOIN llm_cache latest_cache ON latest_cache.comment_id = (
        SELECT rc.id
        FROM review_comments rc
        JOIN llm_cache cache ON cache.comment_id = rc.id
        WHERE rc.thread_id = t.id
        ORDER BY rc.created_at DESC, rc.id DESC
        LIMIT 1
    )
    """


def _annotate_thread(row: sqlite3.Row) -> dict[str, Any]:
    repo = f"{row['owner']}/{row['name']}"
    reviewer = row["reviewer"] or "unknown"
    last_author = row["last_author"]
    pr_author = row["pr_author"]
    waiting_on = pr_author if last_author != pr_author else reviewer
    reviewer_state = "PENDING_AUTHOR" if waiting_on == pr_author else "WAITING_ON_REVIEWER"
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
        "reviewer_state": reviewer_state,
        "blocking": blocking,
        "age_days": age_days,
        "last_activity": last_activity.isoformat() if last_activity else None,
    }


def _count_open_prs(connection: sqlite3.Connection, repo: str | None = None) -> int:
    query = (
        "SELECT COUNT(*) AS total FROM prs p "
        "JOIN repos r ON r.id = p.repo_id "
        "WHERE p.state = 'open'"
    )
    params: list[object] = []
    if repo:
        repo_ref = RepoRef.from_slug(repo)
        query += " AND r.owner = ? AND r.name = ?"
        params.extend([repo_ref.owner, repo_ref.name])
    row = connection.execute(query, params).fetchone()
    return int(row["total"] or 0)


def list_unresolved_threads(
    connection: sqlite3.Connection,
    *,
    repo: str | None = None,
    author: str | None = None,
    label: str | None = None,
    stale_days: int | None = None,
    pending_only: bool = False,
) -> list[dict[str, Any]]:
    query = _base_thread_query() + " WHERE t.thread_state = 'active'"
    params: list[object] = []

    if repo:
        repo_ref = RepoRef.from_slug(repo)
        query += " AND r.owner = ? AND r.name = ?"
        params.extend([repo_ref.owner, repo_ref.name])
    if label:
        query += " AND EXISTS (SELECT 1 FROM pr_labels l WHERE l.pr_id = p.id AND l.label = ?)"
        params.append(label)

    query += " ORDER BY p.updated_at DESC, t.updated_at DESC, t.id DESC"
    rows = connection.execute(query, params).fetchall()
    threads = [_annotate_thread(row) for row in rows]

    if author:
        normalized_author = author.lstrip("@")
        threads = [thread for thread in threads if thread["reviewer"] == normalized_author]
    if pending_only:
        threads = [
            thread for thread in threads if thread["reviewer_state"] == "WAITING_ON_REVIEWER"
        ]
    if stale_days is not None:
        threads = [thread for thread in threads if thread["age_days"] >= stale_days]
    return threads


def list_tracked_repos(
    connection: sqlite3.Connection,
    *,
    repo: str | None = None,
) -> list[dict[str, Any]]:
    query = """
    WITH pr_stats AS (
        SELECT
            p.repo_id,
            COUNT(DISTINCT CASE WHEN p.state = 'open' THEN p.id END) AS open_prs,
            COUNT(DISTINCT CASE WHEN t.thread_state = 'active' THEN t.id END) AS unresolved_threads,
            COUNT(
                DISTINCT CASE
                    WHEN t.thread_state = 'active'
                     AND latest_cache.decision_type = 'REQUEST_CHANGES'
                    THEN t.id
                END
            ) AS blocking_threads
        FROM prs p
        LEFT JOIN review_threads t ON t.pr_id = p.id
        LEFT JOIN llm_cache latest_cache ON latest_cache.comment_id = (
            SELECT rc.id
            FROM review_comments rc
            JOIN llm_cache cache ON cache.comment_id = rc.id
            WHERE rc.thread_id = t.id
            ORDER BY rc.created_at DESC, rc.id DESC
            LIMIT 1
        )
        GROUP BY p.repo_id
    ),
    issue_stats AS (
        SELECT
            i.repo_id,
            COUNT(DISTINCT CASE WHEN i.state = 'open' THEN i.id END) AS open_issues
        FROM issues i
        GROUP BY i.repo_id
    )
    SELECT
        r.owner,
        r.name,
        r.default_branch,
        r.last_synced_at,
        COALESCE(pr_stats.open_prs, 0) AS open_prs,
        COALESCE(pr_stats.unresolved_threads, 0) AS unresolved_threads,
        COALESCE(pr_stats.blocking_threads, 0) AS blocking_threads,
        COALESCE(issue_stats.open_issues, 0) AS open_issues
    FROM repos r
    LEFT JOIN pr_stats ON pr_stats.repo_id = r.id
    LEFT JOIN issue_stats ON issue_stats.repo_id = r.id
    """
    params: list[object] = []
    if repo:
        repo_ref = RepoRef.from_slug(repo)
        query += " WHERE r.owner = ? AND r.name = ?"
        params.extend([repo_ref.owner, repo_ref.name])
    query += " ORDER BY unresolved_threads DESC, open_prs DESC, r.owner, r.name"

    rows = connection.execute(query, params).fetchall()
    return [
        {
            "repo": f"{row['owner']}/{row['name']}",
            "default_branch": row["default_branch"],
            "last_synced_at": row["last_synced_at"],
            "open_prs": int(row["open_prs"] or 0),
            "open_issues": int(row["open_issues"] or 0),
            "unresolved_threads": int(row["unresolved_threads"] or 0),
            "blocking_threads": int(row["blocking_threads"] or 0),
        }
        for row in rows
    ]


def _parse_reference_filter(reference: str, repo: str | None = None) -> dict[str, Any]:
    """Normalize a structured reference search input for SQLite queries."""
    fallback_repo = repo or "placeholder/placeholder"
    references = extract_references(reference, repo=fallback_repo)
    if not references:
        raise ValueError("Reference must be a GitHub URL, owner/repo#123, issue 44, or #123.")

    parsed = references[0]
    if repo is None and parsed.target_repo == fallback_repo and parsed.target_number is not None:
        raise ValueError(
            "Repository is required when using shorthand references like #123 or issue 44."
        )

    return {
        "kind": parsed.kind,
        "url": parsed.url,
        "target_repo": parsed.target_repo,
        "target_number": parsed.target_number,
        "target_sha": parsed.target_sha,
        "raw_text": parsed.raw_text,
    }


def list_repo_issues(
    connection: sqlite3.Connection,
    *,
    repo: str,
    state: str | None = None,
    label: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List issues for a repository with optional state and label filters."""
    repo_ref = RepoRef.from_slug(repo)
    normalized_limit = max(1, limit)
    query = """
    SELECT
        i.number,
        i.title,
        i.state,
        i.author,
        i.updated_at,
        i.body,
        GROUP_CONCAT(DISTINCT l.label) AS labels
    FROM issues i
    JOIN repos r ON r.id = i.repo_id
    LEFT JOIN issue_labels l ON l.issue_id = i.id
    WHERE r.owner = ? AND r.name = ?
    """
    params: list[object] = [repo_ref.owner, repo_ref.name]
    if state:
        query += " AND i.state = ?"
        params.append(state)
    if label:
        query += (
            " AND EXISTS (SELECT 1 FROM issue_labels il WHERE il.issue_id = i.id AND il.label = ?)"
        )
        params.append(label)
    query += " GROUP BY i.id ORDER BY i.updated_at DESC, i.number DESC LIMIT ?"
    params.append(normalized_limit)

    rows = connection.execute(query, params).fetchall()
    return [
        {
            "repo": repo,
            "issue_number": row["number"],
            "title": row["title"],
            "state": row["state"],
            "author": row["author"] or "unknown",
            "updated_at": row["updated_at"],
            "summary": _short_text(row["body"]),
            "labels": sorted((row["labels"] or "").split(",")) if row["labels"] else [],
        }
        for row in rows
    ]


def _reference_exists_clause(
    reference: dict[str, Any], *, source_sql: str
) -> tuple[str, list[object]]:
    """Build a source-scoped EXISTS clause for extracted references."""
    if reference["target_repo"] and reference["target_number"] is not None:
        return (
            f"EXISTS (SELECT 1 FROM extracted_references er WHERE {source_sql} "
            "AND er.target_repo = ? AND er.target_number = ?)",
            [reference["target_repo"], reference["target_number"]],
        )
    if reference["target_repo"] and reference["target_sha"]:
        return (
            f"EXISTS (SELECT 1 FROM extracted_references er WHERE {source_sql} "
            "AND er.target_repo = ? AND er.target_sha = ?)",
            [reference["target_repo"], reference["target_sha"]],
        )
    if reference["url"]:
        return (
            f"EXISTS (SELECT 1 FROM extracted_references er WHERE {source_sql} AND er.url = ?)",
            [reference["url"]],
        )
    return (
        f"EXISTS (SELECT 1 FROM extracted_references er WHERE {source_sql} AND er.raw_text = ?)",
        [reference["raw_text"]],
    )


def search_pull_requests(
    connection: sqlite3.Connection,
    *,
    repo: str | None = None,
    text: str | None = None,
    reference: str | None = None,
    state: str | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Search synced pull requests by free text and/or structured references."""
    normalized_limit = max(1, limit)
    query = """
    SELECT
        r.owner || '/' || r.name AS repo,
        p.number,
        p.title,
        p.state,
        p.author,
        p.updated_at,
        GROUP_CONCAT(DISTINCT l.label) AS labels
    FROM prs p
    JOIN repos r ON r.id = p.repo_id
    LEFT JOIN pr_labels l ON l.pr_id = p.id
    WHERE 1 = 1
    """
    params: list[object] = []

    if repo:
        repo_ref = RepoRef.from_slug(repo)
        query += " AND r.owner = ? AND r.name = ?"
        params.extend([repo_ref.owner, repo_ref.name])
    if state:
        query += " AND p.state = ?"
        params.append(state)
    if text:
        text_like = f"%{text.lower()}%"
        query += """
        AND (
            LOWER(COALESCE(p.title, '')) LIKE ?
            OR LOWER(COALESCE(p.body, '')) LIKE ?
            OR EXISTS (
                SELECT 1
                FROM review_comments c
                JOIN review_threads t ON t.id = c.thread_id
                WHERE t.pr_id = p.id AND LOWER(COALESCE(c.body, '')) LIKE ?
            )
        )
        """
        params.extend([text_like, text_like, text_like])
    if reference:
        parsed_reference = _parse_reference_filter(reference, repo=repo)
        clause, clause_params = _reference_exists_clause(
            parsed_reference,
            source_sql=(
                "((er.source_kind = 'pr' AND er.source_id = p.id) OR "
                "(er.source_kind = 'comment' AND er.source_id IN ("
                "SELECT c2.id FROM review_comments c2 "
                "JOIN review_threads t2 ON t2.id = c2.thread_id WHERE t2.pr_id = p.id"
                ")))"
            ),
        )
        query += f" AND {clause}"
        params.extend(clause_params)

    query += " GROUP BY p.id ORDER BY p.updated_at DESC, p.number DESC LIMIT ?"
    params.append(normalized_limit)
    rows = connection.execute(query, params).fetchall()
    return [
        {
            "repo": row["repo"],
            "number": row["number"],
            "title": row["title"],
            "state": row["state"],
            "author": row["author"] or "unknown",
            "updated_at": row["updated_at"],
            "labels": sorted((row["labels"] or "").split(",")) if row["labels"] else [],
        }
        for row in rows
    ]


def search_issues(
    connection: sqlite3.Connection,
    *,
    repo: str | None = None,
    text: str | None = None,
    reference: str | None = None,
    state: str | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Search synced issues by free text and/or structured references."""
    normalized_limit = max(1, limit)
    query = """
    SELECT
        r.owner || '/' || r.name AS repo,
        i.number,
        i.title,
        i.state,
        i.author,
        i.updated_at,
        GROUP_CONCAT(DISTINCT l.label) AS labels
    FROM issues i
    JOIN repos r ON r.id = i.repo_id
    LEFT JOIN issue_labels l ON l.issue_id = i.id
    WHERE 1 = 1
    """
    params: list[object] = []

    if repo:
        repo_ref = RepoRef.from_slug(repo)
        query += " AND r.owner = ? AND r.name = ?"
        params.extend([repo_ref.owner, repo_ref.name])
    if state:
        query += " AND i.state = ?"
        params.append(state)
    if text:
        text_like = f"%{text.lower()}%"
        query += " AND (LOWER(COALESCE(i.title, '')) LIKE ? OR LOWER(COALESCE(i.body, '')) LIKE ?)"
        params.extend([text_like, text_like])
    if reference:
        parsed_reference = _parse_reference_filter(reference, repo=repo)
        clause, clause_params = _reference_exists_clause(
            parsed_reference,
            source_sql="(er.source_kind = 'issue' AND er.source_id = i.id)",
        )
        query += f" AND {clause}"
        params.extend(clause_params)

    query += " GROUP BY i.id ORDER BY i.updated_at DESC, i.number DESC LIMIT ?"
    params.append(normalized_limit)
    rows = connection.execute(query, params).fetchall()
    return [
        {
            "repo": row["repo"],
            "number": row["number"],
            "title": row["title"],
            "state": row["state"],
            "author": row["author"] or "unknown",
            "updated_at": row["updated_at"],
            "labels": sorted((row["labels"] or "").split(",")) if row["labels"] else [],
        }
        for row in rows
    ]


def search_work_items(
    connection: sqlite3.Connection,
    *,
    repo: str | None = None,
    text: str | None = None,
    reference: str | None = None,
    state: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """Search both pull requests and issues using text and/or reference filters."""
    if not text and not reference:
        raise ValueError("At least one of text or reference must be provided.")
    return {
        "repo": repo,
        "text": text,
        "reference": reference,
        "state": state,
        "pull_requests": search_pull_requests(
            connection,
            repo=repo,
            text=text,
            reference=reference,
            state=state,
            limit=limit,
        ),
        "issues": search_issues(
            connection,
            repo=repo,
            text=text,
            reference=reference,
            state=state,
            limit=limit,
        ),
    }


def get_pr_decisions(
    connection: sqlite3.Connection,
    *,
    repo: str,
    pr_number: int,
) -> list[dict[str, Any]]:
    repo_ref = RepoRef.from_slug(repo)
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
        (repo_ref.owner, repo_ref.name, pr_number),
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


def get_pr_labels(
    connection: sqlite3.Connection,
    *,
    repo: str,
    pr_number: int,
) -> list[str]:
    repo_ref = RepoRef.from_slug(repo)
    rows = connection.execute(
        """
        SELECT l.label
        FROM pr_labels l
        JOIN prs p ON p.id = l.pr_id
        JOIN repos r ON r.id = p.repo_id
        WHERE r.owner = ? AND r.name = ? AND p.number = ?
        ORDER BY l.label ASC
        """,
        (repo_ref.owner, repo_ref.name, pr_number),
    ).fetchall()
    return [str(row["label"]) for row in rows]


def get_issue_labels(
    connection: sqlite3.Connection,
    *,
    repo: str,
    issue_number: int,
) -> list[str]:
    repo_ref = RepoRef.from_slug(repo)
    rows = connection.execute(
        """
        SELECT l.label
        FROM issue_labels l
        JOIN issues i ON i.id = l.issue_id
        JOIN repos r ON r.id = i.repo_id
        WHERE r.owner = ? AND r.name = ? AND i.number = ?
        ORDER BY l.label ASC
        """,
        (repo_ref.owner, repo_ref.name, issue_number),
    ).fetchall()
    return [str(row["label"]) for row in rows]


def _reference_rows_from_query(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [
        {
            "source_kind": row["source_kind"],
            "source_label": row["source_label"],
            "raw_text": row["raw_text"],
            "reference_kind": row["reference_kind"],
            "url": row["url"],
            "target_repo": row["target_repo"],
            "target_number": row["target_number"],
            "target_sha": row["target_sha"],
            "author": row["author"],
            "file_path": row["file_path"],
            "title": row["title"] if "title" in row.keys() else None,
        }
        for row in rows
    ]


def get_pr_references(
    connection: sqlite3.Connection,
    *,
    repo: str,
    pr_number: int,
) -> list[dict[str, Any]]:
    repo_ref = RepoRef.from_slug(repo)
    rows = connection.execute(
        """
        SELECT
            er.source_kind,
            CASE
                WHEN er.source_kind = 'pr' THEN 'PR body'
                ELSE 'Review comment'
            END AS source_label,
            er.raw_text,
            er.reference_kind,
            er.url,
            er.target_repo,
            er.target_number,
            er.target_sha,
            er.title,
            COALESCE(c.author, p.author) AS author,
            t.file_path
        FROM prs p
        JOIN repos r ON r.id = p.repo_id
        LEFT JOIN extracted_references er ON (
            (er.source_kind = 'pr' AND er.source_id = p.id)
            OR (
                er.source_kind = 'comment'
                AND er.source_id IN (
                    SELECT c2.id
                    FROM review_comments c2
                    JOIN review_threads t2 ON t2.id = c2.thread_id
                    WHERE t2.pr_id = p.id
                )
            )
        )
        LEFT JOIN review_comments c ON er.source_kind = 'comment' AND c.id = er.source_id
        LEFT JOIN review_threads t ON c.thread_id = t.id
        WHERE r.owner = ? AND r.name = ? AND p.number = ? AND er.id IS NOT NULL
        ORDER BY er.source_kind ASC, c.created_at ASC, er.id ASC
        """,
        (repo_ref.owner, repo_ref.name, pr_number),
    ).fetchall()
    return _reference_rows_from_query(rows)


def get_issue_references(
    connection: sqlite3.Connection,
    *,
    repo: str,
    issue_number: int,
) -> list[dict[str, Any]]:
    repo_ref = RepoRef.from_slug(repo)
    rows = connection.execute(
        """
        SELECT
            er.source_kind,
            CASE
                WHEN er.source_kind = 'issue' THEN 'Issue body'
                ELSE 'Comment by ' || COALESCE(ic.author, 'unknown')
            END AS source_label,
            er.raw_text,
            er.reference_kind,
            er.url,
            er.target_repo,
            er.target_number,
            er.target_sha,
            er.title,
            COALESCE(i.author, ic.author) AS author,
            NULL AS file_path
        FROM issues i
        JOIN repos r ON r.id = i.repo_id
        LEFT JOIN extracted_references er ON
            (er.source_kind = 'issue' AND er.source_id = i.id)
            OR (
                er.source_kind = 'issue_comment'
                AND er.source_id IN (SELECT id FROM issue_comments WHERE issue_id = i.id)
            )
        LEFT JOIN issue_comments ic ON er.source_kind = 'issue_comment' AND ic.id = er.source_id
        WHERE r.owner = ? AND r.name = ? AND i.number = ? AND er.id IS NOT NULL
        ORDER BY er.source_kind ASC, ic.created_at ASC, er.id ASC
        """,
        (repo_ref.owner, repo_ref.name, issue_number),
    ).fetchall()
    return _reference_rows_from_query(rows)


def get_issue_backreferences(
    connection: sqlite3.Connection,
    *,
    repo: str,
    issue_number: int,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT
            er.source_kind,
            er.raw_text,
            er.reference_kind,
            er.url,
            p.number AS pr_number,
            p.title AS pr_title,
            COALESCE(i.number, ci.number) AS source_issue_number,
            COALESCE(i.title, ci.title) AS source_issue_title,
            COALESCE(c.author, ic.author) AS author,
            t.file_path,
            r.owner || '/' || r.name AS source_repo
        FROM extracted_references er
        LEFT JOIN prs p ON er.source_kind = 'pr' AND p.id = er.source_id
        LEFT JOIN issues i ON er.source_kind = 'issue' AND i.id = er.source_id
        LEFT JOIN review_comments c ON er.source_kind = 'comment' AND c.id = er.source_id
        LEFT JOIN review_threads t ON c.thread_id = t.id
        LEFT JOIN prs cp ON t.pr_id = cp.id
        LEFT JOIN issue_comments ic ON er.source_kind = 'issue_comment' AND ic.id = er.source_id
        LEFT JOIN issues ci ON ic.issue_id = ci.id
        LEFT JOIN repos r ON r.id = COALESCE(p.repo_id, i.repo_id, cp.repo_id, ci.repo_id)
        WHERE er.target_repo = ?
          AND er.target_number = ?
          AND er.reference_kind IN ('issue', 'issue_or_pr')
        ORDER BY er.id ASC
        """,
        (repo, issue_number),
    ).fetchall()

    result: list[dict[str, Any]] = []
    for row in rows:
        if row["source_kind"] == "pr":
            source_label = f"PR #{row['pr_number']} {row['pr_title']}"
        elif row["source_kind"] == "issue":
            source_label = (
                f"Issue #{row['source_issue_number']} {row['source_issue_title']}"
            )
        elif row["source_kind"] == "issue_comment":
            source_label = (
                f"Comment by {row['author'] or 'unknown'} on Issue "
                f"#{row['source_issue_number']} {row['source_issue_title']}"
            )
        else:
            source_label = f"Comment by {row['author'] or 'unknown'}"
        result.append(
            {
                "source_kind": row["source_kind"],
                "source_label": source_label,
                "source_repo": row["source_repo"],
                "raw_text": row["raw_text"],
                "reference_kind": row["reference_kind"],
                "url": row["url"],
                "file_path": row["file_path"],
            }
        )
    return result


def get_repo_sync_status(
    connection: sqlite3.Connection,
    *,
    repo: str,
) -> dict[str, Any]:
    tracked = list_tracked_repos(connection, repo=repo)
    if not tracked:
        raise ValueError(f"Repository {repo} has not been synced yet")
    return tracked[0]


def get_pr_health(
    connection: sqlite3.Connection,
    *,
    repo: str,
    pr_number: int,
) -> PRHealthSummary:
    repo_ref = RepoRef.from_slug(repo)
    pr_row = connection.execute(
        """
        SELECT p.title, p.state, p.author, p.updated_at
        FROM prs p
        JOIN repos r ON r.id = p.repo_id
        WHERE r.owner = ? AND r.name = ? AND p.number = ?
        """,
        (repo_ref.owner, repo_ref.name, pr_number),
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


def get_issue_comments(
    connection: sqlite3.Connection,
    *,
    repo: str,
    issue_number: int,
) -> list[dict[str, Any]]:
    repo_ref = RepoRef.from_slug(repo)
    rows = connection.execute(
        """
        SELECT ic.author, ic.body, ic.created_at, ic.updated_at, ic.reaction_count
        FROM issue_comments ic
        JOIN issues i ON ic.issue_id = i.id
        JOIN repos r ON i.repo_id = r.id
        WHERE r.owner = ? AND r.name = ? AND i.number = ?
        ORDER BY ic.created_at ASC
        """,
        (repo_ref.owner, repo_ref.name, issue_number),
    ).fetchall()
    return [dict(row) for row in rows]


def get_issue_context_payload(
    connection: sqlite3.Connection,
    *,
    repo: str,
    issue_number: int,
) -> dict[str, Any]:
    repo_ref = RepoRef.from_slug(repo)
    row = connection.execute(
        """
        SELECT i.title, i.state, i.author, i.created_at, i.updated_at, i.closed_at, i.body
        FROM issues i
        JOIN repos r ON r.id = i.repo_id
        WHERE r.owner = ? AND r.name = ? AND i.number = ?
        """,
        (repo_ref.owner, repo_ref.name, issue_number),
    ).fetchone()
    if not row:
        raise ValueError(f"Issue #{issue_number} not found for {repo}")

    return {
        "repo": repo,
        "issue_number": issue_number,
        "title": row["title"],
        "state": row["state"],
        "author": row["author"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "closed_at": row["closed_at"],
        "body": row["body"] or "",
        "labels": get_issue_labels(connection, repo=repo, issue_number=issue_number),
        "references": get_issue_references(connection, repo=repo, issue_number=issue_number),
        "mentioned_by": get_issue_backreferences(connection, repo=repo, issue_number=issue_number),
        "comments": get_issue_comments(connection, repo=repo, issue_number=issue_number),
        "repo_status": get_repo_sync_status(connection, repo=repo),
    }


def get_pr_context_payload(
    connection: sqlite3.Connection,
    *,
    repo: str,
    pr_number: int,
) -> dict[str, Any]:
    health = get_pr_health(connection, repo=repo, pr_number=pr_number)
    unresolved_threads = [
        thread
        for thread in list_unresolved_threads(connection, repo=repo)
        if thread["pr_number"] == pr_number
    ]
    return {
        "repo": repo,
        "pr_number": pr_number,
        "health": health.model_dump(mode="json"),
        "decisions": get_pr_decisions(connection, repo=repo, pr_number=pr_number),
        "unresolved_threads": unresolved_threads,
        "labels": get_pr_labels(connection, repo=repo, pr_number=pr_number),
        "references": get_pr_references(connection, repo=repo, pr_number=pr_number),
        "repo_status": get_repo_sync_status(connection, repo=repo),
    }


def _filtered_repo_breakdown(
    connection: sqlite3.Connection,
    *,
    unresolved_threads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    tracked_by_repo = {row["repo"]: row for row in list_tracked_repos(connection)}
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "repo": "",
            "open_pr_numbers": set(),
            "unresolved_threads": 0,
            "blocking_threads": 0,
            "last_synced_at": None,
            "open_issues": 0,
        }
    )

    for thread in unresolved_threads:
        row = grouped[thread["repo"]]
        row["repo"] = thread["repo"]
        row["open_pr_numbers"].add(thread["pr_number"])
        row["unresolved_threads"] += 1
        row["blocking_threads"] += int(thread["blocking"])
        tracked_repo = tracked_by_repo.get(thread["repo"], {})
        row["last_synced_at"] = tracked_repo.get("last_synced_at")
        row["open_issues"] = tracked_repo.get("open_issues", 0)

    return [
        {
            "repo": repo_name,
            "open_prs": len(values["open_pr_numbers"]),
            "unresolved_threads": values["unresolved_threads"],
            "blocking_threads": values["blocking_threads"],
            "open_issues": values["open_issues"],
            "last_synced_at": values["last_synced_at"],
        }
        for repo_name, values in sorted(grouped.items())
    ]


def get_dashboard_summary(
    connection: sqlite3.Connection,
    *,
    repo: str | None = None,
    reviewer: str | None = None,
    label: str | None = None,
    stale_days: int = 7,
) -> dict[str, Any]:
    normalized_reviewer = reviewer.lstrip("@") if reviewer else None
    unresolved_threads = list_unresolved_threads(
        connection,
        repo=repo,
        author=normalized_reviewer,
        label=label,
    )
    stale_threads = [thread for thread in unresolved_threads if thread["age_days"] >= stale_days]
    blocking_threads = [thread for thread in unresolved_threads if thread["blocking"]]

    use_filtered_totals = normalized_reviewer is not None or label is not None
    if use_filtered_totals:
        repo_breakdown = _filtered_repo_breakdown(connection, unresolved_threads=unresolved_threads)
        open_prs = len({(thread["repo"], thread["pr_number"]) for thread in unresolved_threads})
        repos_tracked = len({thread["repo"] for thread in unresolved_threads})
    else:
        tracked_repos = list_tracked_repos(connection, repo=repo)
        repo_breakdown = [
            {
                "repo": repo_row["repo"],
                "open_prs": repo_row["open_prs"],
                "unresolved_threads": repo_row["unresolved_threads"],
                "blocking_threads": repo_row["blocking_threads"],
                "open_issues": repo_row["open_issues"],
                "last_synced_at": repo_row["last_synced_at"],
            }
            for repo_row in tracked_repos
        ]
        repos_tracked = len(tracked_repos)
        open_prs = _count_open_prs(connection, repo=repo)

    reviewer_counter = Counter(
        thread["reviewer"] for thread in unresolved_threads if thread["reviewer"] != "unknown"
    )
    blocking_counter = Counter(
        thread["reviewer"] for thread in blocking_threads if thread["reviewer"] != "unknown"
    )
    reviewer_load = [
        {
            "reviewer": reviewer_name,
            "unresolved_threads": count,
            "blocking_threads": blocking_counter.get(reviewer_name, 0),
        }
        for reviewer_name, count in reviewer_counter.most_common()
    ]

    return {
        "repo": repo,
        "reviewer": normalized_reviewer,
        "label": label,
        "repos_tracked": repos_tracked,
        "open_prs": open_prs,
        "unresolved_threads": len(unresolved_threads),
        "blocking_threads": len(blocking_threads),
        "stale_threads": len(stale_threads),
        "stale_days": stale_days,
        "repo_breakdown": repo_breakdown,
        "reviewer_load": reviewer_load,
    }


def get_reviewer_status(
    connection: sqlite3.Connection,
    *,
    repo: str | None = None,
    reviewer: str,
) -> dict[str, Any]:
    normalized_reviewer = reviewer.lstrip("@")
    threads = list_unresolved_threads(connection, repo=repo, author=normalized_reviewer)
    blocking = [thread for thread in threads if thread["blocking"]]
    pending = [thread for thread in threads if thread["reviewer_state"] == "WAITING_ON_REVIEWER"]
    waiting_on_author = [
        thread for thread in threads if thread["reviewer_state"] == "PENDING_AUTHOR"
    ]
    return {
        "repo": repo or "all",
        "reviewer": normalized_reviewer,
        "unresolved_threads": len(threads),
        "blocking_threads": len(blocking),
        "pending_threads": len(pending),
        "waiting_on_author_threads": len(waiting_on_author),
        "threads": threads,
    }


def list_resolved_pr_decisions(
    connection: sqlite3.Connection,
    *,
    repo: str,
    pr_number: int,
) -> list[dict[str, Any]]:
    """List resolved architectural decisions made on a PR, across all files."""
    repo_ref = RepoRef.from_slug(repo)
    query = """
    SELECT
        t.file_path,
        rc.author AS reviewer,
        rc.body AS raw_text,
        dl.decision_status,
        dl.decision_reason,
        dl.extracted_summary,
        rc.created_at
    FROM decision_log dl
    JOIN review_comments rc ON rc.id = dl.comment_id
    JOIN review_threads t ON t.id = rc.thread_id
    JOIN prs p ON p.id = t.pr_id
    JOIN repos r ON r.id = p.repo_id
    WHERE r.owner = ? AND r.name = ?
    AND p.number = ?
    AND t.thread_state != 'active'
    ORDER BY rc.created_at ASC
    """
    rows = connection.execute(
        query, (repo_ref.owner, repo_ref.name, pr_number)
    ).fetchall()
    return [
        {
            "file_path": row["file_path"],
            "reviewer": row["reviewer"],
            "raw_text": row["raw_text"],
            "decision_status": row["decision_status"],
            "decision_reason": row["decision_reason"],
            "extracted_summary": row["extracted_summary"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def get_file_implementation_summary(
    connection: sqlite3.Connection,
    *,
    repo: str,
    pr_number: int,
    file_path: str,
) -> str | None:
    """Fetch the synthesized implementation summary for a specific file in a PR."""
    repo_ref = RepoRef.from_slug(repo)
    query = """
    SELECT s.summary
    FROM implementation_summaries s
    JOIN repos r ON r.id = s.repo_id
    JOIN prs p ON p.id = s.target_id
    WHERE r.owner = ? AND r.name = ?
      AND s.target_type = 'pr'
      AND p.number = ?
      AND s.file_path = ?
    ORDER BY s.generated_at DESC
    LIMIT 1
    """
    row = connection.execute(
        query, (repo_ref.owner, repo_ref.name, pr_number, file_path)
    ).fetchone()
    return row["summary"] if row else None
