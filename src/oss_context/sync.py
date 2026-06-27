from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from prcontext.db import DatabaseManager
from prcontext.github import GitHubClient
from prcontext.intelligence import analyze_pending_comments
from prcontext.models import (
    PullRequestData,
    RepoRef,
    ReviewCommentData,
    ReviewThreadData,
    SyncReport,
)
from prcontext.settings import Settings


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def _upsert_repo(
    connection: sqlite3.Connection, repo: RepoRef, github_id: int, default_branch: str | None
) -> tuple[int, datetime | None]:
    row = connection.execute(
        "SELECT id, last_synced_at FROM repos WHERE owner = ? AND name = ?",
        (repo.owner, repo.name),
    ).fetchone()
    if row:
        connection.execute(
            "UPDATE repos SET github_id = ?, default_branch = ? WHERE id = ?",
            (github_id, default_branch, row["id"]),
        )
        last_synced = (
            datetime.fromisoformat(row["last_synced_at"]).astimezone(UTC)
            if row["last_synced_at"]
            else None
        )
        return row["id"], last_synced

    cursor = connection.execute(
        """
        INSERT INTO repos(github_id, owner, name, default_branch, last_synced_at)
        VALUES(?, ?, ?, ?, NULL)
        """,
        (github_id, repo.owner, repo.name, default_branch),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("Failed to insert repo row")
    return cursor.lastrowid, None


def _upsert_pr(connection: sqlite3.Connection, repo_id: int, pr: PullRequestData) -> int:
    row = connection.execute(
        "SELECT id FROM prs WHERE repo_id = ? AND number = ?",
        (repo_id, pr.number),
    ).fetchone()
    payload = (
        pr.github_id,
        pr.title,
        pr.state,
        pr.author,
        _iso(pr.created_at),
        _iso(pr.updated_at),
        pr.body,
        pr.base_branch,
        pr.head_branch,
        pr.merge_commit_sha,
        repo_id,
        pr.number,
    )

    if row:
        connection.execute(
            """
            UPDATE prs
            SET github_id = ?, title = ?, state = ?, author = ?, created_at = ?, updated_at = ?,
                body = ?, base_branch = ?, head_branch = ?, merge_commit_sha = ?
            WHERE repo_id = ? AND number = ?
            """,
            payload,
        )
        pr_id = row["id"]
    else:
        cursor = connection.execute(
            """
            INSERT INTO prs(
                github_id, title, state, author, created_at, updated_at, body,
                base_branch, head_branch, merge_commit_sha, repo_id, number
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            payload,
        )
        if cursor.lastrowid is None:
            raise RuntimeError("Failed to insert PR row")
        pr_id = cursor.lastrowid

    connection.execute("DELETE FROM pr_labels WHERE pr_id = ?", (pr_id,))
    connection.executemany(
        "INSERT INTO pr_labels(pr_id, label, added_at) VALUES(?, ?, ?)",
        [(pr_id, label, _iso(datetime.now(UTC))) for label in pr.labels],
    )
    return pr_id


def _upsert_thread(connection: sqlite3.Connection, pr_id: int, thread: ReviewThreadData) -> int:
    row = connection.execute(
        "SELECT id FROM review_threads WHERE github_thread_id = ?",
        (thread.github_thread_id,),
    ).fetchone()
    payload = (
        pr_id,
        thread.file_path,
        thread.line_number,
        thread.thread_state,
        thread.resolved_by,
        _iso(thread.resolved_at),
        _iso(thread.created_at),
        _iso(thread.updated_at),
        thread.github_thread_id,
    )
    if row:
        connection.execute(
            """
            UPDATE review_threads
            SET pr_id = ?, file_path = ?, line_number = ?, thread_state = ?, resolved_by = ?,
                resolved_at = ?, created_at = ?, updated_at = ?
            WHERE github_thread_id = ?
            """,
            payload,
        )
        return row["id"]

    cursor = connection.execute(
        """
        INSERT INTO review_threads(
            pr_id, file_path, line_number, thread_state, resolved_by,
            resolved_at, created_at, updated_at, github_thread_id
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )
    if cursor.lastrowid is None:
        raise RuntimeError("Failed to insert review thread row")
    return cursor.lastrowid


def _upsert_comment(
    connection: sqlite3.Connection, thread_id: int, comment: ReviewCommentData
) -> int:
    existing = connection.execute(
        "SELECT id, body FROM review_comments WHERE github_comment_id = ?",
        (comment.github_comment_id,),
    ).fetchone()
    payload = (
        thread_id,
        comment.author,
        comment.body,
        _iso(comment.created_at),
        _iso(comment.updated_at),
        comment.reaction_count,
        int(comment.is_suggestion),
        int(comment.suggestion_applied),
        comment.github_comment_id,
    )
    if existing:
        body_changed = (existing["body"] or "") != comment.body
        connection.execute(
            """
            UPDATE review_comments
            SET thread_id = ?, author = ?, body = ?, created_at = ?, updated_at = ?,
                reaction_count = ?, is_suggestion = ?, suggestion_applied = ?,
                extracted_decision = CASE WHEN ? THEN NULL ELSE extracted_decision END,
                decision_confidence = CASE WHEN ? THEN NULL ELSE decision_confidence END
            WHERE github_comment_id = ?
            """,
            payload[:-1] + (int(body_changed), int(body_changed), comment.github_comment_id),
        )
        if body_changed:
            connection.execute("DELETE FROM llm_cache WHERE comment_id = ?", (existing["id"],))
        return existing["id"]

    cursor = connection.execute(
        """
        INSERT INTO review_comments(
            thread_id, author, body, created_at, updated_at, reaction_count,
            is_suggestion, suggestion_applied, github_comment_id
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )
    if cursor.lastrowid is None:
        raise RuntimeError("Failed to insert review comment row")
    return cursor.lastrowid


async def sync_repository(
    repo_slug: str,
    settings: Settings,
    *,
    extract_decisions: bool = True,
    batch_size: int = 10,
) -> SyncReport:
    repo = RepoRef.from_slug(repo_slug)
    report = SyncReport(repo=repo.slug, started_at=datetime.now(UTC))
    database = DatabaseManager(settings.db_path)
    connection = database.initialize()

    async with GitHubClient(settings) as client:
        repo_payload = await client.get_repo(repo)
        repo_id, last_synced_at = _upsert_repo(
            connection,
            repo,
            github_id=repo_payload["id"],
            default_branch=repo_payload.get("default_branch"),
        )

        async for pull_request in client.iter_pull_requests(repo, since=last_synced_at):
            pr_id = _upsert_pr(connection, repo_id, pull_request)
            report.prs_synced += 1

            threads = await client.fetch_review_threads(repo, pull_request.number)
            for thread in threads:
                thread_id = _upsert_thread(connection, pr_id, thread)
                report.threads_synced += 1
                for comment in thread.comments:
                    _upsert_comment(connection, thread_id, comment)
                    report.comments_synced += 1

            connection.commit()

        connection.execute(
            "UPDATE repos SET last_synced_at = ? WHERE id = ?",
            (_iso(datetime.now(UTC)), repo_id),
        )
        connection.commit()

        if extract_decisions:
            report.decisions_extracted = await analyze_pending_comments(
                connection,
                settings,
                repo_id=repo_id,
                batch_size=batch_size,
            )

    report.finished_at = datetime.now(UTC)
    connection.close()
    return report
