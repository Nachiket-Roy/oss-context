"""Rich terminal rendering helpers for oss-context output.

This module turns sync reports, unresolved thread lists, extracted decisions,
and PR health summaries into readable tables and panels for CLI users.
"""

from __future__ import annotations

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from oss_context.models import PRHealthSummary, SyncReport


def render_sync_report(report: SyncReport) -> Panel:
    table = Table.grid(padding=(0, 2))
    table.add_row("Repo", report.repo)
    table.add_row("PRs synced", str(report.prs_synced))
    table.add_row("Threads synced", str(report.threads_synced))
    table.add_row("Comments synced", str(report.comments_synced))
    table.add_row("Decisions extracted", str(report.decisions_extracted))
    if report.finished_at:
        duration = report.finished_at - report.started_at
        table.add_row("Duration", str(duration).split(".")[0])
    return Panel(table, title="Sync complete", border_style="green")


def render_unresolved_threads(rows: list[dict]) -> Panel:
    if not rows:
        return Panel(
            "No unresolved threads found.", title="Unresolved threads", border_style="green"
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

        pr_title = f"#{row['pr_number']} {row['pr_title']}"
        table.add_row(
            row["repo"],
            pr_title,
            row["file_path"],
            row["reviewer"],
            decision,
            row["waiting_on"],
            row["summary"],
        )

    return Panel(table, title="Unresolved threads", border_style="yellow")


def render_decisions(rows: list[dict], *, repo: str, pr_number: int) -> Panel:
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
