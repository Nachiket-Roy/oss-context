"""Decision extraction orchestration for synced review comments.

This module finds comments that still need analysis, batches them through the
configured classifier, updates cached extraction state, and records decision
history in SQLite.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime

from oss_context.llm import LLMClassifier
from oss_context.models import CommentForAnalysis
from oss_context.settings import Settings


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _body_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


async def analyze_pending_comments(
    connection: sqlite3.Connection,
    settings: Settings,
    *,
    repo_id: int | None = None,
    batch_size: int = 10,
) -> int:
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than zero")

    where_clauses = ["TRIM(COALESCE(c.body, '')) != ''"]
    params: list[object] = []
    if repo_id is not None:
        where_clauses.append("p.repo_id = ?")
        params.append(repo_id)

    rows = connection.execute(
        f"""
        SELECT
            c.id AS comment_id,
            c.body,
            c.extracted_decision,
            cache.input_hash,
            t.file_path,
            p.number AS pr_number,
            r.owner || '/' || r.name AS repo
        FROM review_comments c
        JOIN review_threads t ON t.id = c.thread_id
        JOIN prs p ON p.id = t.pr_id
        JOIN repos r ON r.id = p.repo_id
        LEFT JOIN llm_cache cache ON cache.comment_id = c.id
        WHERE {" AND ".join(where_clauses)}
        ORDER BY c.created_at ASC, c.id ASC
        """,
        params,
    ).fetchall()

    pending: list[CommentForAnalysis] = []
    for row in rows:
        comment_hash = _body_hash(row["body"] or "")
        # For backfills: even if the decision was already extracted and the hash matches,
        # we may still need to backfill missing decision_status if the schema was just migrated.
        # But to avoid re-running the LLM for every row, we check if decision_status is NULL in the log.
        log_row = connection.execute("SELECT decision_status FROM decision_log WHERE comment_id = ? AND raw_text_hash = ?", (row["comment_id"], comment_hash)).fetchone()
        needs_backfill = log_row and log_row["decision_status"] is None
        
        if row["extracted_decision"] and row["input_hash"] == comment_hash and not needs_backfill:
            continue
        pending.append(
            CommentForAnalysis(
                comment_id=row["comment_id"],
                body=row["body"] or "",
                file_path=row["file_path"],
                pr_number=row["pr_number"],
                repo=row["repo"],
            )
        )

    if not pending:
        return 0

    classifier = LLMClassifier(settings)
    extracted_count = 0

    for index in range(0, len(pending), batch_size):
        batch = pending[index : index + batch_size]
        results = await classifier.classify(batch)
        analyzed_at = _now_iso()

        for comment in batch:
            decision = results[comment.comment_id]
            input_hash = _body_hash(comment.body)
            thread_row = connection.execute(
                "SELECT thread_id FROM review_comments WHERE id = ?",
                (comment.comment_id,),
            ).fetchone()
            pr_row = connection.execute(
                "SELECT pr_id FROM review_threads WHERE id = ?",
                (thread_row["thread_id"],),
            ).fetchone()

            connection.execute(
                """
                UPDATE review_comments
                SET extracted_decision = ?, decision_confidence = ?
                WHERE id = ?
                """,
                (decision.decision_type, decision.confidence, comment.comment_id),
            )
            connection.execute(
                """
                INSERT INTO llm_cache(
                    comment_id, provider, model, input_hash,
                    decision_type, summary, confidence, analyzed_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(comment_id) DO UPDATE SET
                    provider = excluded.provider,
                    model = excluded.model,
                    input_hash = excluded.input_hash,
                    decision_type = excluded.decision_type,
                    summary = excluded.summary,
                    confidence = excluded.confidence,
                    analyzed_at = excluded.analyzed_at
                """,
                (
                    comment.comment_id,
                    decision.provider,
                    decision.model,
                    input_hash,
                    decision.decision_type,
                    decision.summary,
                    decision.confidence,
                    analyzed_at,
                ),
            )

            existing_log = connection.execute(
                "SELECT id, decision_status, decision_reason FROM decision_log WHERE comment_id = ? AND raw_text_hash = ?",
                (comment.comment_id, input_hash),
            ).fetchone()
            
            if not existing_log:
                connection.execute(
                    """
                    INSERT INTO decision_log(
                        pr_id, comment_id, decision_type, extracted_summary,
                        raw_text, raw_text_hash, extracted_at, decision_status, decision_reason
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pr_row["pr_id"],
                        comment.comment_id,
                        decision.decision_type,
                        decision.summary,
                        comment.body,
                        input_hash,
                        analyzed_at,
                        decision.status,
                        decision.reason,
                    ),
                )
            elif existing_log["decision_status"] is None or existing_log["decision_reason"] is None:
                # Backfill missing fields from existing analysis
                connection.execute(
                    """
                    UPDATE decision_log 
                    SET decision_status = COALESCE(decision_status, ?),
                        decision_reason = COALESCE(decision_reason, ?)
                    WHERE id = ?
                    """,
                    (decision.status, decision.reason, existing_log["id"])
                )
            extracted_count += 1

    connection.commit()
    return extracted_count
