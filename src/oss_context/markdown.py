"""Markdown rendering helpers for MCP resources and tool responses.

This module formats repository, PR, reviewer, and dashboard state into compact
markdown that can be consumed directly by MCP clients and agentic IDEs.
"""

from __future__ import annotations

from oss_context.models import PRHealthSummary


def _bullet_list(items: list[str]) -> str:
    if not items:
        return "- none"
    return "\n".join(f"- {item}" for item in items)


def _thread_lines(rows: list[dict]) -> str:
    if not rows:
        return "- No unresolved threads"
    lines: list[str] = []
    for row in rows:
        marker = "blocking" if row["blocking"] else row["decision_type"].lower()
        lines.append(
            f"- `{row['file_path']}` · reviewer `{row['reviewer']}` · {marker} · "
            f"waiting on `{row['waiting_on']}` · {row['summary']}"
        )
    return "\n".join(lines)


def render_pr_context_markdown(payload: dict) -> str:
    health = PRHealthSummary.model_validate(payload["health"])
    labels = payload["labels"]
    repo_status = payload["repo_status"]
    return "\n".join(
        [
            f"# PR #{payload['pr_number']} Context · {payload['repo']}",
            "",
            f"Last synced: {repo_status['last_synced_at'] or 'never'}",
            f"State: {health.state}",
            f"Author: {health.author or 'unknown'}",
            f"Health score: {health.health_score}",
            f"Unresolved threads: {health.unresolved_threads}",
            f"Blocking threads: {health.blocking_threads}",
            "",
            "## Labels",
            _bullet_list([f"`{label}`" for label in labels]),
            "",
            "## Unresolved threads",
            _thread_lines(payload["unresolved_threads"]),
            "",
            "## Decision history",
            _bullet_list(
                [
                    f"`{row['author']}` · {row['decision_type']} · {row['summary']}"
                    for row in payload["decisions"]
                ]
            ),
        ]
    )


def render_unresolved_threads_markdown(rows: list[dict], *, title: str) -> str:
    return "\n".join([f"# {title}", "", _thread_lines(rows)])


def render_reviewer_status_markdown(status: dict) -> str:
    return "\n".join(
        [
            f"# Reviewer status · {status['reviewer']}",
            "",
            f"Scope: {status['repo']}",
            f"Unresolved threads: {status['unresolved_threads']}",
            f"Blocking threads: {status['blocking_threads']}",
            f"Waiting on reviewer: {status['pending_threads']}",
            f"Waiting on author: {status['waiting_on_author_threads']}",
            "",
            "## Threads",
            _thread_lines(status["threads"]),
        ]
    )


def render_dashboard_markdown(summary: dict) -> str:
    repo_lines = [
        (
            f"`{row['repo']}` · open PRs {row['open_prs']} "
            f"· unresolved {row['unresolved_threads']} "
            f"· blocking {row['blocking_threads']} "
            f"· synced {row['last_synced_at'] or 'never'}"
        )
        for row in summary["repo_breakdown"]
    ]
    reviewer_lines = [
        (
            f"`{row['reviewer']}` · unresolved {row['unresolved_threads']} "
            f"· blocking {row['blocking_threads']}"
        )
        for row in summary["reviewer_load"]
    ]
    return "\n".join(
        [
            "# Dashboard overview",
            "",
            f"Scope: {summary['repo'] or 'all repos'}",
            f"Repos tracked: {summary['repos_tracked']}",
            f"Open PRs: {summary['open_prs']}",
            f"Unresolved threads: {summary['unresolved_threads']}",
            f"Blocking threads: {summary['blocking_threads']}",
            f"Stale threads (≥{summary['stale_days']}d): {summary['stale_threads']}",
            "",
            "## Repo breakdown",
            _bullet_list(repo_lines),
            "",
            "## Reviewer load",
            _bullet_list(reviewer_lines),
        ]
    )


def render_repo_sync_markdown(status: dict) -> str:
    return "\n".join(
        [
            f"# Repo sync status · {status['repo']}",
            "",
            f"Default branch: {status['default_branch'] or 'unknown'}",
            f"Last synced: {status['last_synced_at'] or 'never'}",
            f"Open PRs: {status['open_prs']}",
            f"Unresolved threads: {status['unresolved_threads']}",
            f"Blocking threads: {status['blocking_threads']}",
        ]
    )
