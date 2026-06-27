"""Rich terminal rendering helpers for oss-context output.

This module turns sync reports, unresolved thread lists, extracted decisions,
PR and issue context payloads, reviewer status, and dashboard summaries into
readable panels and tables for CLI users.
"""

from __future__ import annotations

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from oss_context.models import PRHealthSummary, SyncReport


def render_sync_report(report: SyncReport) -> Panel:
    """Render the result of a repository sync."""
    table = Table.grid(padding=(0, 2))
    table.add_row("Repo", report.repo)
    table.add_row("PRs synced", str(report.prs_synced))
    table.add_row("Issues synced", str(report.issues_synced))
    table.add_row("Threads synced", str(report.threads_synced))
    table.add_row("Comments synced", str(report.comments_synced))
    table.add_row("Decisions extracted", str(report.decisions_extracted))
    table.add_row("References extracted", str(report.references_extracted))
    if report.finished_at:
        duration = report.finished_at - report.started_at
        table.add_row("Duration", str(duration).split(".")[0])
    return Panel(table, title="Sync complete", border_style="green")


def render_unresolved_threads(rows: list[dict]) -> Panel:
    """Render unresolved review threads."""
    if not rows:
        return Panel(
            "No unresolved threads found.",
            title="Unresolved threads",
            border_style="green",
        )

    table = Table(show_lines=False)
    table.add_column("Repo", style="cyan", no_wrap=True)
    table.add_column("PR", style="bold")
    table.add_column("File")
    table.add_column("Reviewer")
    table.add_column("Decision")
    table.add_column("Waiting on")
    table.add_column("Summary", overflow="fold")

    for row in rows:
        decision = row["decision_type"]
        if row["blocking"]:
            decision = f"⚠ {decision}"
        elif decision == "QUESTION":
            decision = f"? {decision}"
        elif decision == "SUGGESTION":
            decision = f"💡 {decision}"

        table.add_row(
            row["repo"],
            f"#{row['pr_number']} {row['pr_title']}",
            row["file_path"],
            row["reviewer"],
            decision,
            row["waiting_on"],
            row["summary"],
        )

    return Panel(table, title="Unresolved threads", border_style="yellow")


def render_decisions(rows: list[dict], *, repo: str, pr_number: int) -> Panel:
    """Render extracted decisions for a pull request."""
    if not rows:
        return Panel(
            f"No extracted decisions found for {repo} PR #{pr_number}.",
            title="Decisions",
            border_style="yellow",
        )

    table = Table(show_lines=False)
    table.add_column("Author")
    table.add_column("Decision")
    table.add_column("Confidence", justify="right")
    table.add_column("File")
    table.add_column("Summary", overflow="fold")

    for row in rows:
        table.add_row(
            row["author"],
            row["decision_type"],
            f"{row['confidence']:.2f}",
            row["file_path"],
            row["summary"],
        )

    return Panel(table, title=f"Decisions · {repo} PR #{pr_number}", border_style="blue")


def render_pr_health(summary: PRHealthSummary) -> Panel:
    """Render the PR health summary view."""
    metrics = Table.grid(padding=(0, 2))
    metrics.add_row("Repo", summary.repo)
    metrics.add_row("PR", f"#{summary.pr_number} {summary.title}")
    metrics.add_row("State", summary.state)
    metrics.add_row("Author", summary.author or "unknown")
    metrics.add_row("Health score", str(summary.health_score))
    metrics.add_row("Unresolved threads", str(summary.unresolved_threads))
    metrics.add_row("Blocking threads", str(summary.blocking_threads))
    metrics.add_row("Approvals", str(summary.approvals))
    metrics.add_row("Questions", str(summary.questions))
    metrics.add_row("Suggestions", str(summary.suggestions))
    metrics.add_row("Acknowledgments", str(summary.acknowledgments))

    states = Table(show_header=True)
    states.add_column("Reviewer")
    states.add_column("Decision")
    states.add_column("State")
    states.add_column("Waiting on")
    states.add_column("File")

    for row in summary.reviewer_states:
        states.add_row(
            row["reviewer"],
            row["decision"],
            row["state"],
            row["waiting_on"],
            row["file_path"],
        )

    content = Group(metrics, Text(""), states)
    border_style = (
        "green" if summary.health_score >= 80 else "yellow" if summary.health_score >= 50 else "red"
    )
    return Panel(content, title="PR health", border_style=border_style)


def _render_reference_table(rows: list[dict], *, title: str) -> Table | Text:
    """Render linked references for PR and issue context views."""
    if not rows:
        return Text(f"No {title.lower()}.")

    table = Table(show_header=True)
    table.add_column("Source")
    table.add_column("Kind")
    table.add_column("Target", overflow="fold")

    for row in rows:
        target = row["url"] or row["target_repo"] or row["raw_text"]
        if row["target_number"] is not None and row["target_repo"]:
            target = f"{row['target_repo']}#{row['target_number']}"
        elif row["target_sha"] is not None and row["target_repo"]:
            target = f"{row['target_repo']}@{row['target_sha']}"
        table.add_row(row["source_label"], row["reference_kind"], target)

    table.title = title
    return table


def _render_mentions_table(rows: list[dict]) -> Table | Text:
    """Render issue backreferences."""
    if not rows:
        return Text("No inbound mentions.")

    table = Table(show_header=True)
    table.add_column("Mentioned by")
    table.add_column("Repo")
    table.add_column("File")

    for row in rows:
        table.add_row(row["source_label"], row["source_repo"], row["file_path"] or "—")

    table.title = "Mentioned by"
    return table


def render_pr_context(payload: dict) -> Panel:
    """Render a full PR context view including references."""
    health = PRHealthSummary.model_validate(payload["health"])

    metrics = Table.grid(padding=(0, 2))
    metrics.add_row("Repo", payload["repo"])
    metrics.add_row("PR", f"#{payload['pr_number']} {health.title}")
    metrics.add_row("State", health.state)
    metrics.add_row("Author", health.author or "unknown")
    metrics.add_row("Last synced", payload["repo_status"]["last_synced_at"] or "never")
    metrics.add_row("Labels", ", ".join(payload["labels"]) if payload["labels"] else "—")
    metrics.add_row("Health score", str(health.health_score))
    metrics.add_row("Unresolved threads", str(health.unresolved_threads))
    metrics.add_row("Blocking threads", str(health.blocking_threads))

    thread_table: Table | Text
    if payload["unresolved_threads"]:
        thread_table = Table(show_header=True)
        thread_table.title = "Unresolved threads"
        thread_table.add_column("File")
        thread_table.add_column("Reviewer")
        thread_table.add_column("Decision")
        thread_table.add_column("Waiting on")
        thread_table.add_column("Summary", overflow="fold")
        for row in payload["unresolved_threads"]:
            thread_table.add_row(
                row["file_path"],
                row["reviewer"],
                row["decision_type"],
                row["waiting_on"],
                row["summary"],
            )
    else:
        thread_table = Text("No unresolved threads.")

    decision_table: Table | Text
    if payload["decisions"]:
        decision_table = Table(show_header=True)
        decision_table.title = "Decision history"
        decision_table.add_column("Author")
        decision_table.add_column("Decision")
        decision_table.add_column("Confidence", justify="right")
        decision_table.add_column("File")
        decision_table.add_column("Summary", overflow="fold")
        for row in payload["decisions"]:
            decision_table.add_row(
                row["author"],
                row["decision_type"],
                f"{row['confidence']:.2f}",
                row["file_path"],
                row["summary"],
            )
    else:
        decision_table = Text("No decision history found.")

    content = Group(
        metrics,
        Text(""),
        _render_reference_table(payload["references"], title="Linked references"),
        Text(""),
        thread_table,
        Text(""),
        decision_table,
    )
    border_style = "red" if health.blocking_threads else "cyan"
    return Panel(content, title="PR context", border_style=border_style)


def render_issue_context(payload: dict) -> Panel:
    """Render a full issue context view including outbound and inbound references."""
    metrics = Table.grid(padding=(0, 2))
    metrics.add_row("Repo", payload["repo"])
    metrics.add_row("Issue", f"#{payload['issue_number']} {payload['title']}")
    metrics.add_row("State", payload["state"])
    metrics.add_row("Author", payload["author"] or "unknown")
    metrics.add_row("Last synced", payload["repo_status"]["last_synced_at"] or "never")
    metrics.add_row("Labels", ", ".join(payload["labels"]) if payload["labels"] else "—")

    body = Text(payload["body"] or "No issue body.")
    content = Group(
        metrics,
        Text(""),
        Text("Body", style="bold"),
        body,
        Text(""),
        _render_reference_table(payload["references"], title="Outbound references"),
        Text(""),
        _render_mentions_table(payload["mentioned_by"]),
    )
    return Panel(content, title="Issue context", border_style="cyan")


def render_tracked_repos(rows: list[dict]) -> Panel:
    """Render tracked repository freshness and aggregate counts."""
    if not rows:
        return Panel("No synced repositories found.", title="Tracked repos", border_style="yellow")

    table = Table(show_lines=False)
    table.add_column("Repo", style="cyan", no_wrap=True)
    table.add_column("Default branch")
    table.add_column("Open PRs", justify="right")
    table.add_column("Open issues", justify="right")
    table.add_column("Unresolved", justify="right")
    table.add_column("Blocking", justify="right")
    table.add_column("Last synced")

    for row in rows:
        table.add_row(
            row["repo"],
            row["default_branch"] or "—",
            str(row["open_prs"]),
            str(row.get("open_issues", 0)),
            str(row["unresolved_threads"]),
            str(row["blocking_threads"]),
            row["last_synced_at"] or "never",
        )

    return Panel(table, title="Tracked repos", border_style="blue")


def render_reviewer_status(status: dict) -> Panel:
    """Render reviewer-centric waiting and blocking state."""
    metrics = Table.grid(padding=(0, 2))
    metrics.add_row("Scope", status["repo"])
    metrics.add_row("Reviewer", status["reviewer"])
    metrics.add_row("Unresolved threads", str(status["unresolved_threads"]))
    metrics.add_row("Blocking threads", str(status["blocking_threads"]))
    metrics.add_row("Waiting on reviewer", str(status["pending_threads"]))
    metrics.add_row("Waiting on author", str(status["waiting_on_author_threads"]))

    return Panel(metrics, title="Reviewer status", border_style="magenta")


def render_dashboard(summary: dict) -> Panel:
    """Render the cross-repo dashboard summary."""
    metrics = Table.grid(padding=(0, 2))
    metrics.add_row("Repo scope", summary["repo"] or "all repos")
    metrics.add_row("Reviewer scope", summary["reviewer"] or "all reviewers")
    metrics.add_row("Label scope", summary.get("label") or "all labels")
    metrics.add_row("Repos tracked", str(summary["repos_tracked"]))
    metrics.add_row("Open PRs", str(summary["open_prs"]))
    metrics.add_row("Unresolved threads", str(summary["unresolved_threads"]))
    metrics.add_row("Blocking threads", str(summary["blocking_threads"]))
    metrics.add_row(f"Stale threads (≥{summary['stale_days']}d)", str(summary["stale_threads"]))

    repo_table = Table(show_header=True)
    repo_table.add_column("Repo", style="cyan")
    repo_table.add_column("Open PRs", justify="right")
    repo_table.add_column("Unresolved", justify="right")
    repo_table.add_column("Blocking", justify="right")
    repo_table.add_column("Last synced")

    for row in summary["repo_breakdown"]:
        repo_table.add_row(
            row["repo"],
            str(row["open_prs"]),
            str(row["unresolved_threads"]),
            str(row["blocking_threads"]),
            row["last_synced_at"] or "never",
        )

    reviewer_table = Table(show_header=True)
    reviewer_table.add_column("Reviewer")
    reviewer_table.add_column("Unresolved", justify="right")
    reviewer_table.add_column("Blocking", justify="right")

    for row in summary["reviewer_load"]:
        reviewer_table.add_row(
            row["reviewer"],
            str(row["unresolved_threads"]),
            str(row["blocking_threads"]),
        )

    content = Group(metrics, Text(""), repo_table, Text(""), reviewer_table)
    border_style = "red" if summary["blocking_threads"] else "green"
    return Panel(content, title="Dashboard", border_style=border_style)
