"""Markdown rendering helpers for MCP resources and tool responses.

This module formats repository, PR, issue, reviewer, dashboard, and linked
reference state into compact markdown that can be consumed directly by MCP
clients and agentic IDEs. All user-controlled text is escaped before being
inserted into markdown.
"""

from __future__ import annotations

from oss_context.models import PRHealthSummary


def _md_escape(value: str | None) -> str:
    if not value:
        return ""
    compact = " ".join(value.split())
    escaped = compact.replace("\\", "\\\\")
    for char in ("`", "*", "_", "{", "}", "[", "]", "(", ")", "#", "+", "-", "!", ">", "|"):
        escaped = escaped.replace(char, f"\\{char}")
    return escaped


def _bullet_list(items: list[str]) -> str:
    if not items:
        return "- none"
    return "\n".join(f"- {item}" for item in items)


def _reference_target(reference: dict) -> str:
    if reference["url"]:
        return _md_escape(reference["url"])
    target_repo = _md_escape(reference["target_repo"])
    if reference["target_number"] is not None:
        return f"{target_repo}#{reference['target_number']}"
    if reference["target_sha"]:
        return f"{target_repo}@{_md_escape(reference['target_sha'])}"
    return _md_escape(reference["raw_text"])


def _reference_lines(rows: list[dict]) -> str:
    if not rows:
        return "- none"
    lines: list[str] = []
    for row in rows:
        source = _md_escape(row.get("source_label") or row["source_kind"])
        target = _reference_target(row)
        lines.append(f"- {source} → {target} ({_md_escape(row['reference_kind'])})")
    return "\n".join(lines)


def _thread_lines(rows: list[dict]) -> str:
    if not rows:
        return "- No unresolved threads"
    lines: list[str] = []
    for row in rows:
        marker = "blocking" if row["blocking"] else row["decision_type"].lower()
        lines.append(
            f"- `{_md_escape(row['file_path'])}` · reviewer `{_md_escape(row['reviewer'])}` · "
            f"{_md_escape(marker)} · waiting on `{_md_escape(row['waiting_on'])}` · "
            f"{_md_escape(row['summary'])}"
        )
    return "\n".join(lines)


def render_pr_context_markdown(payload: dict) -> str:
    health = PRHealthSummary.model_validate(payload["health"])
    labels = payload["labels"]
    repo_status = payload["repo_status"]
    return "\n".join(
        [
            f"# PR #{payload['pr_number']} Context · {_md_escape(payload['repo'])}",
            "",
            f"Last synced: {_md_escape(repo_status['last_synced_at'] or 'never')}",
            f"State: {_md_escape(health.state)}",
            f"Author: {_md_escape(health.author or 'unknown')}",
            f"Health score: {health.health_score}",
            f"Unresolved threads: {health.unresolved_threads}",
            f"Blocking threads: {health.blocking_threads}",
            "",
            "## Labels",
            _bullet_list([f"`{_md_escape(label)}`" for label in labels]),
            "",
            "## Unresolved threads",
            _thread_lines(payload["unresolved_threads"]),
            "",
            "## Linked references",
            _reference_lines(payload["references"]),
            "",
            "## Decision history",
            _bullet_list(
                [
                    f"`{_md_escape(row['author'])}` · {_md_escape(row['decision_type'])} · "
                    f"{_md_escape(row['summary'])}"
                    for row in payload["decisions"]
                ]
            ),
        ]
    )


def render_issue_context_markdown(payload: dict) -> str:
    return "\n".join(
        [
            f"# Issue #{payload['issue_number']} Context · {_md_escape(payload['repo'])}",
            "",
            f"Title: {_md_escape(payload['title'])}",
            f"State: {_md_escape(payload['state'])}",
            f"Author: {_md_escape(payload['author'] or 'unknown')}",
            f"Last synced: {_md_escape(payload['repo_status']['last_synced_at'] or 'never')}",
            "",
            "## Labels",
            _bullet_list([f"`{_md_escape(label)}`" for label in payload["labels"]]),
            "",
            "## Body",
            _bullet_list([_md_escape(payload["body"])]) if payload["body"] else "- empty",
            "",
            "## Outbound references",
            _reference_lines(payload["references"]),
            "",
            "## Mentioned by",
            _bullet_list(
                [
                    f"{_md_escape(row['source_repo'])} · {_md_escape(row['source_label'])}"
                    for row in payload["mentioned_by"]
                ]
            ),
        ]
    )


def render_unresolved_threads_markdown(rows: list[dict], *, title: str) -> str:
    return "\n".join([f"# {_md_escape(title)}", "", _thread_lines(rows)])


def render_reviewer_status_markdown(status: dict) -> str:
    return "\n".join(
        [
            f"# Reviewer status · {_md_escape(status['reviewer'])}",
            "",
            f"Scope: {_md_escape(status['repo'])}",
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
            f"`{_md_escape(row['repo'])}` · open PRs {row['open_prs']} "
            f"· unresolved {row['unresolved_threads']} "
            f"· blocking {row['blocking_threads']} "
            f"· synced {_md_escape(row['last_synced_at'] or 'never')}"
        )
        for row in summary["repo_breakdown"]
    ]
    reviewer_lines = [
        (
            f"`{_md_escape(row['reviewer'])}` · unresolved {row['unresolved_threads']} "
            f"· blocking {row['blocking_threads']}"
        )
        for row in summary["reviewer_load"]
    ]
    scope = _md_escape(summary["repo"] or "all repos")
    reviewer_scope = _md_escape(summary["reviewer"] or "all reviewers")
    label_scope = _md_escape(summary.get("label") or "all labels")
    return "\n".join(
        [
            "# Dashboard overview",
            "",
            f"Repo scope: {scope}",
            f"Reviewer scope: {reviewer_scope}",
            f"Label scope: {label_scope}",
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
            f"# Repo sync status · {_md_escape(status['repo'])}",
            "",
            f"Default branch: {_md_escape(status['default_branch'] or 'unknown')}",
            f"Last synced: {_md_escape(status['last_synced_at'] or 'never')}",
            f"Open PRs: {status['open_prs']}",
            f"Open issues: {status.get('open_issues', 0)}",
            f"Unresolved threads: {status['unresolved_threads']}",
            f"Blocking threads: {status['blocking_threads']}",
        ]
    )
