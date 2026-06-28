"""Repository sync pipeline for oss-context.

This module pulls GitHub pull-request and issue data into SQLite, upserts
repository state, persists review threads and comments, extracts structured
references from bodies and comments, and optionally triggers decision extraction
after the sync completes.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from oss_context.db import DatabaseManager
from oss_context.github import GitHubClient
from oss_context.intelligence import analyze_pending_comments
from oss_context.models import (
    IssueData,
    PullRequestData,
    RepoRef,
    ReviewCommentData,
    ReviewThreadData,
    SyncReport,
)
from oss_context.references import extract_references
from oss_context.settings import Settings


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def _upsert_repo(
    connection: sqlite3.Connection,
    repo: RepoRef,
    github_id: int,
    default_branch: str | None,
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


def _replace_references(
    connection: sqlite3.Connection,
    *,
    repo_id: int,
    repo_slug: str,
    source_kind: str,
    source_id: int,
    text: str | None,
) -> int:
    connection.execute(
        "DELETE FROM extracted_references WHERE source_kind = ? AND source_id = ?",
        (source_kind, source_id),
    )

    references = extract_references(text, repo=repo_slug)
    if not references:
        return 0

    connection.executemany(
        """
        INSERT INTO extracted_references(
            source_kind, source_id, repo_id, reference_kind, raw_text, url,
            target_repo, target_number, target_sha
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                source_kind,
                source_id,
                repo_id,
                reference.kind,
                reference.raw_text,
                reference.url,
                reference.target_repo,
                reference.target_number,
                reference.target_sha,
            )
            for reference in references
        ],
    )
    return len(references)


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


def _upsert_issue(connection: sqlite3.Connection, repo_id: int, issue: IssueData) -> int:
    row = connection.execute(
        "SELECT id FROM issues WHERE repo_id = ? AND number = ?",
        (repo_id, issue.number),
    ).fetchone()
    payload = (
        issue.github_id,
        issue.title,
        issue.state,
        issue.author,
        _iso(issue.created_at),
        _iso(issue.updated_at),
        _iso(issue.closed_at),
        issue.body,
        repo_id,
        issue.number,
    )

    if row:
        connection.execute(
            """
            UPDATE issues
            SET github_id = ?, title = ?, state = ?, author = ?, created_at = ?, updated_at = ?,
                closed_at = ?, body = ?
            WHERE repo_id = ? AND number = ?
            """,
            payload,
        )
        issue_id = row["id"]
    else:
        cursor = connection.execute(
            """
            INSERT INTO issues(
                github_id, title, state, author, created_at, updated_at,
                closed_at, body, repo_id, number
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            payload,
        )
        if cursor.lastrowid is None:
            raise RuntimeError("Failed to insert issue row")
        issue_id = cursor.lastrowid

    connection.execute("DELETE FROM issue_labels WHERE issue_id = ?", (issue_id,))
    connection.executemany(
        "INSERT INTO issue_labels(issue_id, label, added_at) VALUES(?, ?, ?)",
        [(issue_id, label, _iso(datetime.now(UTC))) for label in issue.labels],
    )
    return issue_id


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
    connection: sqlite3.Connection,
    thread_id: int,
    comment: ReviewCommentData,
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
    limit: int | None = 100,
    since_override: datetime | None = None,
) -> SyncReport:
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than zero")

    repo = RepoRef.from_slug(repo_slug)
    print(f"Syncing {repo.slug}...", flush=True)

    report = SyncReport(repo=repo.slug, started_at=datetime.now(UTC))
    database = DatabaseManager(settings.db_path)
    connection = database.initialize()

    try:
        async with GitHubClient(settings) as client:
            repo_payload = await client.get_repo(repo)
            repo_id_val = repo_payload["id"]
            default_branch_val = repo_payload.get("default_branch")

            if since_override is not None:
                last_synced_at = since_override
            else:
                # Resolve last_synced_at from DB
                row = connection.execute(
                    "SELECT last_synced_at FROM repos WHERE owner = ? AND name = ?",
                    (repo.owner, repo.name),
                ).fetchone()
                last_synced_at = (
                    datetime.fromisoformat(row["last_synced_at"]).astimezone(UTC)
                    if row and row["last_synced_at"]
                    else None
                )

            # Register/upsert repository slug
            repo_id, last_synced_at = _upsert_repo(
                connection,
                repo,
                github_id=repo_id_val,
                default_branch=default_branch_val,
            )
            connection.commit()

            print("Fetching PRs...", flush=True)
            prs_buffer = []
            synced_prs_info = []
            async for pull_request in client.iter_pull_requests(repo, since=last_synced_at):
                prs_buffer.append(pull_request)
                if limit and len(synced_prs_info) + len(prs_buffer) >= limit:
                    remaining = limit - len(synced_prs_info)
                    prs_buffer = prs_buffer[:remaining]
                    for pr in prs_buffer:
                        pr_id = _upsert_pr(connection, repo_id, pr)
                        synced_prs_info.append((pr_id, pr.number))
                        report.prs_synced += 1
                        report.references_extracted += _replace_references(
                            connection,
                            repo_id=repo_id,
                            repo_slug=repo.slug,
                            source_kind="pr",
                            source_id=pr_id,
                            text=pr.body,
                        )
                    connection.commit()
                    prs_buffer.clear()

                    if client.pr_total_estimate:
                        print(
                            f"Fetched {report.prs_synced}/{client.pr_total_estimate} PRs",
                            flush=True,
                        )
                    else:
                        print(f"Fetched {report.prs_synced} PRs", flush=True)
                    break

                if len(prs_buffer) == 100:
                    for pr in prs_buffer:
                        pr_id = _upsert_pr(connection, repo_id, pr)
                        synced_prs_info.append((pr_id, pr.number))
                        report.prs_synced += 1
                        report.references_extracted += _replace_references(
                            connection,
                            repo_id=repo_id,
                            repo_slug=repo.slug,
                            source_kind="pr",
                            source_id=pr_id,
                            text=pr.body,
                        )
                    connection.commit()
                    prs_buffer.clear()

                    if client.pr_total_estimate:
                        print(
                            f"Fetched {report.prs_synced}/{client.pr_total_estimate} PRs",
                            flush=True,
                        )
                    else:
                        print(f"Fetched {report.prs_synced} PRs", flush=True)

            if prs_buffer:
                for pr in prs_buffer:
                    pr_id = _upsert_pr(connection, repo_id, pr)
                    synced_prs_info.append((pr_id, pr.number))
                    report.prs_synced += 1
                    report.references_extracted += _replace_references(
                        connection,
                        repo_id=repo_id,
                        repo_slug=repo.slug,
                        source_kind="pr",
                        source_id=pr_id,
                        text=pr.body,
                    )
                connection.commit()
                prs_buffer.clear()

            if report.prs_synced == 0:
                print("Fetched 0 PRs", flush=True)
            elif report.prs_synced % 100 != 0:
                if client.pr_total_estimate:
                    print(
                        f"Fetched {report.prs_synced}/{client.pr_total_estimate} PRs",
                        flush=True,
                    )
                else:
                    print(f"Fetched {report.prs_synced} PRs", flush=True)

            print("Fetching review threads...", flush=True)
            for index, (pr_id, pr_number) in enumerate(synced_prs_info, start=1):
                threads = await client.fetch_review_threads(repo, pr_number)
                for thread in threads:
                    thread_id = _upsert_thread(connection, pr_id, thread)
                    report.threads_synced += 1
                    for comment in thread.comments:
                        comment_id = _upsert_comment(connection, thread_id, comment)
                        report.comments_synced += 1
                        report.references_extracted += _replace_references(
                            connection,
                            repo_id=repo_id,
                            repo_slug=repo.slug,
                            source_kind="comment",
                            source_id=comment_id,
                            text=comment.body,
                        )

                if index % 50 == 0:
                    connection.commit()

                if index % 100 == 0 or index == len(synced_prs_info):
                    print(
                        f"Fetched review threads for {index}/{len(synced_prs_info)} PRs",
                        flush=True,
                    )

            connection.commit()

            print("Fetching issues...", flush=True)
            issues_buffer = []
            async for issue in client.iter_issues(repo, since=last_synced_at):
                issues_buffer.append(issue)
                if limit and report.issues_synced + len(issues_buffer) >= limit:
                    remaining = limit - report.issues_synced
                    issues_buffer = issues_buffer[:remaining]
                    for iss in issues_buffer:
                        issue_id = _upsert_issue(connection, repo_id, iss)
                        report.issues_synced += 1
                        report.references_extracted += _replace_references(
                            connection,
                            repo_id=repo_id,
                            repo_slug=repo.slug,
                            source_kind="issue",
                            source_id=issue_id,
                            text=iss.body,
                        )
                    connection.commit()
                    issues_buffer.clear()

                    if client.issue_total_estimate:
                        print(
                            f"Fetched {report.issues_synced}/{client.issue_total_estimate} issues",
                            flush=True,
                        )
                    else:
                        print(f"Fetched {report.issues_synced} issues", flush=True)
                    break

                if len(issues_buffer) == 100:
                    for iss in issues_buffer:
                        issue_id = _upsert_issue(connection, repo_id, iss)
                        report.issues_synced += 1
                        report.references_extracted += _replace_references(
                            connection,
                            repo_id=repo_id,
                            repo_slug=repo.slug,
                            source_kind="issue",
                            source_id=issue_id,
                            text=iss.body,
                        )
                    connection.commit()
                    issues_buffer.clear()

                    if client.issue_total_estimate:
                        print(
                            f"Fetched {report.issues_synced}/{client.issue_total_estimate} issues",
                            flush=True,
                        )
                    else:
                        print(f"Fetched {report.issues_synced} issues", flush=True)

            if issues_buffer:
                for iss in issues_buffer:
                    issue_id = _upsert_issue(connection, repo_id, iss)
                    report.issues_synced += 1
                    report.references_extracted += _replace_references(
                        connection,
                        repo_id=repo_id,
                        repo_slug=repo.slug,
                        source_kind="issue",
                        source_id=issue_id,
                        text=iss.body,
                    )
                connection.commit()
                issues_buffer.clear()

            if report.issues_synced == 0:
                print("Fetched 0 issues", flush=True)
            elif report.issues_synced % 100 != 0:
                if client.issue_total_estimate:
                    print(
                        f"Fetched {report.issues_synced}/{client.issue_total_estimate} issues",
                        flush=True,
                    )
                else:
                    print(f"Fetched {report.issues_synced} issues", flush=True)

            print("Writing to database...", flush=True)
            connection.execute(
                "UPDATE repos SET last_synced_at = ? WHERE id = ?",
                (_iso(report.started_at), repo_id),
            )
            connection.commit()

            if extract_decisions:
                report.decisions_extracted = await analyze_pending_comments(
                    connection,
                    settings,
                    repo_id=repo_id,
                    batch_size=batch_size,
                )

            print("Done.", flush=True)

        report.finished_at = datetime.now(UTC)
        return report
    finally:
        connection.close()
