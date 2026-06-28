"""Deterministic retrieval provenance and diagnostics for oss-context.

This module centralizes confidence labels, retrieval reasons, provenance payloads,
and doctor-style checks so context results can explain why they were returned and
contributors can validate retrieval quality over time.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any, Literal

ConfidenceLevel = Literal["HIGH", "MEDIUM", "LOW"]


def build_provenance(
    *,
    source_type: str,
    source_id: str,
    confidence: ConfidenceLevel,
    retrieval_reason: str,
    reason_detail: str,
) -> dict[str, str]:
    """Build a normalized provenance payload for a retrieved context item."""
    return {
        "source_type": source_type,
        "source_id": source_id,
        "confidence": confidence,
        "retrieval_reason": retrieval_reason,
        "reason_detail": reason_detail,
    }


def summarize_provenance(items: list[dict[str, Any]]) -> list[str]:
    """Collapse repeated provenance records into human-readable explanation lines."""
    seen: set[tuple[str, str, str]] = set()
    lines: list[str] = []
    for item in items:
        provenance = item.get("provenance")
        if not isinstance(provenance, dict):
            continue
        key = (
            str(provenance.get("retrieval_reason") or ""),
            str(provenance.get("confidence") or ""),
            str(provenance.get("reason_detail") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        lines.append(
            f"{provenance['confidence']}: {provenance['retrieval_reason']} — "
            f"{provenance['reason_detail']}"
        )
    return lines


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO timestamp into UTC for retrieval diagnostics."""
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(UTC)


def _file_path_matches(indexed_path: str, requested_path: str) -> bool:
    """Match repo-relative file paths with exact or suffix semantics."""
    normalized_indexed = indexed_path.replace("\\", "/")
    normalized_requested = requested_path.replace("\\", "/")
    return (
        normalized_indexed == normalized_requested
        or normalized_indexed.endswith(f"/{normalized_requested}")
        or normalized_requested.endswith(f"/{normalized_indexed}")
    )


def run_retrieval_doctor(connection: sqlite3.Connection) -> dict[str, Any]:
    """Inspect common retrieval-quality problems in the local SQLite database."""
    stale_branch_links = [
        {
            "repo": row["repo_slug"],
            "branch": row["branch_name"],
            "pr_number": row["pr_number"],
        }
        for row in connection.execute(
            """
            SELECT bl.repo_slug, bl.branch_name, bl.pr_number
            FROM branch_links bl
            LEFT JOIN repos r
              ON r.owner || '/' || r.name = bl.repo_slug
            LEFT JOIN prs p
              ON p.repo_id = r.id AND p.number = bl.pr_number
            WHERE p.id IS NULL
            ORDER BY bl.repo_slug, bl.branch_name
            """
        ).fetchall()
    ]

    latest_snapshots = {
        str(row["repo_slug"]): row
        for row in connection.execute(
            """
            SELECT s.repo_slug, s.repo_root, s.indexed_at
            FROM code_index_snapshots s
            JOIN (
                SELECT repo_slug, MAX(id) AS max_id
                FROM code_index_snapshots
                WHERE repo_slug IS NOT NULL
                GROUP BY repo_slug
            ) latest ON latest.max_id = s.id
            """
        ).fetchall()
        if row["repo_slug"]
    }
    repos = [
        {
            "repo": f"{row['owner']}/{row['name']}",
            "last_synced_at": row["last_synced_at"],
        }
        for row in connection.execute(
            "SELECT owner, name, last_synced_at FROM repos ORDER BY owner, name"
        ).fetchall()
    ]
    missing_code_indexes = [repo for repo in repos if repo["repo"] not in latest_snapshots]

    outdated_code_indexes: list[dict[str, Any]] = []
    for repo in repos:
        snapshot = latest_snapshots.get(repo["repo"])
        if snapshot is None:
            continue
        indexed_at = _parse_iso(snapshot["indexed_at"])
        last_synced_at = _parse_iso(repo["last_synced_at"])
        if indexed_at is not None and last_synced_at is not None and indexed_at < last_synced_at:
            outdated_code_indexes.append(
                {
                    "repo": repo["repo"],
                    "indexed_at": snapshot["indexed_at"],
                    "last_synced_at": repo["last_synced_at"],
                }
            )

    orphaned_file_references: list[dict[str, Any]] = []
    thread_rows = connection.execute(
        """
        SELECT
            r.owner || '/' || r.name AS repo,
            p.number AS pr_number,
            t.github_thread_id,
            t.file_path
        FROM review_threads t
        JOIN prs p ON p.id = t.pr_id
        JOIN repos r ON r.id = p.repo_id
        WHERE t.file_path IS NOT NULL
        ORDER BY repo, p.number, t.id
        """
    ).fetchall()
    for row in thread_rows:
        snapshot = latest_snapshots.get(str(row["repo"]))
        if snapshot is None:
            continue
        indexed_files = connection.execute(
            """
            SELECT f.file_path
            FROM code_index_files f
            JOIN code_index_snapshots s ON s.id = f.snapshot_id
            WHERE s.repo_slug = ? AND s.id = (
                SELECT MAX(id) FROM code_index_snapshots WHERE repo_slug = ?
            )
            """,
            (row["repo"], row["repo"]),
        ).fetchall()
        if not any(
            _file_path_matches(str(file_row["file_path"]), str(row["file_path"]))
            for file_row in indexed_files
        ):
            orphaned_file_references.append(
                {
                    "repo": row["repo"],
                    "pr_number": row["pr_number"],
                    "thread_id": row["github_thread_id"],
                    "file_path": row["file_path"],
                }
            )

    checks = {
        "stale_branch_links": stale_branch_links,
        "missing_code_indexes": missing_code_indexes,
        "outdated_code_indexes": outdated_code_indexes,
        "orphaned_file_references": orphaned_file_references,
    }
    return {
        "healthy": all(not values for values in checks.values()),
        **checks,
    }
