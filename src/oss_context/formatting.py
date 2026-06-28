"""Rich terminal rendering helpers for oss-context output.

This module turns sync reports, unresolved thread lists, extracted decisions,
PR and issue context payloads, branch-aware workflow data, reviewer status, and
dashboard summaries into readable panels and tables for CLI users.
"""

from __future__ import annotations

from rich.console import Group, RenderableType
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


def render_branch_resolution(payload: dict) -> Panel:
    """Render how a branch was resolved to a pull request."""
    metrics = Table.grid(padding=(0, 2))
    metrics.add_row("Repo", payload["repo"])
    metrics.add_row("Branch", payload["branch"])
    metrics.add_row("PR", f"#{payload['pr_number']}")
    metrics.add_row("Resolution", payload["source"].replace("_", " "))
    metrics.add_row("Repo root", str(payload["repo_root"]))
    return Panel(metrics, title="Current branch PR", border_style="cyan")


def render_branch_context(payload: dict) -> Group:
    """Render branch metadata followed by the resolved PR context."""
    resolution = render_branch_resolution(
        {
            "repo": payload["repo"],
            "branch": payload["branch"],
            "pr_number": payload["pr_number"],
            "source": payload["resolution_source"],
            "repo_root": payload["repo_root"],
        }
    )
    parts: list[RenderableType] = [resolution, Text(""), render_pr_context(payload["pr_context"])]
    if payload.get("explain"):
        explain_text = Text(
            "Returned because:\n"
            + "\n".join(f"- {line}" for line in payload["retrieval_explanations"])
        )
        parts.extend(
            [Text(""), Panel(explain_text, title="Retrieval explanation", border_style="magenta")]
        )
    return Group(*parts)


def render_branch_file_context(payload: dict) -> Panel:
    """Render unresolved review state for a single file on the current branch PR."""
    metrics = Table.grid(padding=(0, 2))
    metrics.add_row("Repo", payload["repo"])
    metrics.add_row("Branch", payload["branch"])
    metrics.add_row("PR", f"#{payload['pr_number']}")
    metrics.add_row("File", payload["file_path"])
    metrics.add_row("Resolution", payload["resolution_source"].replace("_", " "))

    def _build_history_text(history: list[dict]) -> Text:
        lines = ["Resolved review history:\n"]
        for row in history:
            lines.append(f"- {row['reviewer']}:")
            text = "  " + "\n  ".join(row['raw_text'].splitlines())
            lines.append(text + "\n")
            if row['decision_status'] in ('ACCEPTED', 'REJECTED'):
                lines.append("  Status:")
                lines.append(f"  {row['decision_status'].capitalize()}.\n")
            else:
                outcome = row.get('extracted_summary') or row.get('decision_reason') or row['decision_status']
                outcome_text = "  " + "\n  ".join(str(outcome).splitlines())
                lines.append("  Outcome:")
                lines.append(f"{outcome_text}\n")
        return Text("\n".join(lines).strip())

    if not payload["threads"]:
        body: list[RenderableType] = [
            metrics,
            Text(""),
            Text("No unresolved threads for this file."),
        ]
        if payload.get("resolved_history"):
            body.extend([Text(""), _build_history_text(payload["resolved_history"])])
        if payload.get("references"):
            ref_lines = Text(
                "Linked references:\n"
                + "\n".join(
                    (
                        f"- {row['source_label']} → "
                        f"{row.get('target_repo') or row.get('url') or row['raw_text']}"
                    )
                    for row in payload["references"]
                )
            )
            body.extend([Text(""), ref_lines])
        if payload.get("explain"):
            explain = Text(
                "Returned because:\n"
                + "\n".join(f"- {line}" for line in payload["retrieval_explanations"])
                + "\n\nExcluded:\n"
                + "\n".join(f"- {line}" for line in payload["excluded"])
            )
            body.extend(
                [Text(""), Panel(explain, title="Retrieval explanation", border_style="magenta")]
            )
        return Panel(Group(*body), title="File context", border_style="green")

    table = Table(show_header=True)
    table.add_column("Reviewer")
    table.add_column("Decision")
    table.add_column("Waiting on")
    table.add_column("Confidence")
    table.add_column("Reason")
    table.add_column("Summary", overflow="fold")
    for row in payload["threads"]:
        provenance = row["provenance"]
        table.add_row(
            row["reviewer"],
            row["decision_type"],
            row["waiting_on"],
            provenance["confidence"],
            provenance["retrieval_reason"],
            row["summary"],
        )

    sections: list[RenderableType] = [metrics, Text(""), table]
    if payload.get("resolved_history"):
        sections.extend([Text(""), _build_history_text(payload["resolved_history"])])
    if payload.get("references"):
        ref_table = Table(show_header=True)
        ref_table.title = "Linked references"
        ref_table.add_column("Kind")
        ref_table.add_column("Target")
        ref_table.add_column("Confidence")
        ref_table.add_column("Reason")
        for row in payload["references"]:
            provenance = row["provenance"]
            target = row.get("target_repo") or row.get("url") or row["raw_text"]
            if row.get("target_repo") and row.get("target_number") is not None:
                target = f"{row['target_repo']}#{row['target_number']}"
            ref_table.add_row(
                row["reference_kind"],
                str(target),
                provenance["confidence"],
                provenance["retrieval_reason"],
            )
        sections.extend([Text(""), ref_table])
    if payload.get("explain"):
        explain = Text(
            "Returned because:\n"
            + "\n".join(f"- {line}" for line in payload["retrieval_explanations"])
            + "\n\nExcluded:\n"
            + "\n".join(f"- {line}" for line in payload["excluded"])
        )
        sections.extend(
            [Text(""), Panel(explain, title="Retrieval explanation", border_style="magenta")]
        )
    return Panel(Group(*sections), title="File context", border_style="yellow")


def render_hook_installation(paths: list[str]) -> Panel:
    """Render the result of installing git hooks."""
    body = Text("Installed hooks:\n" + "\n".join(f"- {path}" for path in paths))
    return Panel(body, title="Git hooks installed", border_style="green")


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


def render_code_index_report(report: dict) -> Panel:
    """Render a local code-index run summary."""
    metrics = Table.grid(padding=(0, 2))
    metrics.add_row("Repo", report["repo"] or "local workspace")
    metrics.add_row("Repo root", report["repo_root"])
    metrics.add_row("Branch", report["branch"] or "detached/unknown")
    metrics.add_row("Commit", report["commit"] or "unknown")
    metrics.add_row("Snapshot", str(report["snapshot_id"]))
    metrics.add_row("Files indexed", str(report["files_indexed"]))
    metrics.add_row("Files parsed", str(report["files_parsed"]))
    metrics.add_row("Files reused", str(report["files_reused"]))
    metrics.add_row("Symbols", str(report["symbols_indexed"]))
    metrics.add_row("Calls", str(report["calls_indexed"]))
    metrics.add_row("Indexed at", report["indexed_at"])
    if report["reused_snapshot"]:
        metrics.add_row("Mode", "reused existing snapshot")
    if report["skipped_files"]:
        metrics.add_row("Skipped files", ", ".join(report["skipped_files"][:5]))
    return Panel(metrics, title="Code index", border_style="cyan")


def render_symbol_search(rows: list[dict], *, title: str) -> Panel:
    """Render symbol search results."""
    if not rows:
        return Panel("No indexed symbols found.", title=title, border_style="yellow")

    table = Table(show_header=True)
    table.add_column("Repo")
    table.add_column("Branch")
    table.add_column("Kind")
    table.add_column("Symbol")
    table.add_column("File")
    table.add_column("Line", justify="right")
    for row in rows:
        table.add_row(
            row["repo"],
            row["branch"] or "—",
            row.get("kind", "—"),
            row.get("qualified_name") or row.get("caller") or row.get("callee") or "—",
            row["file_path"],
            str(row.get("line_number") or "—"),
        )
    return Panel(table, title=title, border_style="blue")


def render_impacted_files(rows: list[dict], *, symbol: str) -> Panel:
    """Render files impacted by a symbol and its direct callers."""
    if not rows:
        return Panel(
            "No impacted files found.", title=f"Impacted files · {symbol}", border_style="yellow"
        )

    table = Table(show_header=True)
    table.add_column("Repo")
    table.add_column("Branch")
    table.add_column("File")
    table.add_column("Reasons", overflow="fold")
    for row in rows:
        table.add_row(
            row["repo"],
            row["branch"] or "—",
            row["file_path"],
            ", ".join(row["reasons"]),
        )
    return Panel(table, title=f"Impacted files · {symbol}", border_style="magenta")


def render_file_context_report(payload: dict) -> Panel:
    """Render indexed file context plus historical review context."""
    metrics = Table.grid(padding=(0, 2))
    metrics.add_row("Repo", payload["repo"] or "local workspace")
    metrics.add_row("File", payload["file_path"])
    metrics.add_row("Branch", payload["branch"] or "detached/unknown")
    metrics.add_row("Commit", payload["commit"] or "unknown")
    metrics.add_row("Indexed at", payload["indexed_at"])

    symbol_table = Table(show_header=True)
    symbol_table.title = "Defined symbols"
    symbol_table.add_column("Kind")
    symbol_table.add_column("Qualified name")
    symbol_table.add_column("Line", justify="right")
    for row in payload["symbols"]:
        symbol_table.add_row(row["kind"], row["qualified_name"], str(row["line_number"] or "—"))

    outbound_table = Table(show_header=True)
    outbound_table.title = "Outgoing calls"
    outbound_table.add_column("Callee")
    outbound_table.add_column("Count", justify="right")
    outbound_table.add_column("First line", justify="right")
    for row in payload["outbound_calls"]:
        outbound_table.add_row(
            row["callee"],
            str(row["call_count"]),
            str(row["first_line"] or "—"),
        )

    inbound_table = Table(show_header=True)
    inbound_table.title = "Inbound calls"
    inbound_table.add_column("Repo")
    inbound_table.add_column("File")
    inbound_table.add_column("Caller")
    inbound_table.add_column("Line", justify="right")
    for row in payload["inbound_calls"]:
        inbound_table.add_row(
            row["repo"],
            row["file_path"],
            row["caller"],
            str(row["line_number"] or "—"),
        )

    history_table = Table(show_header=True)
    history_table.title = "Review history"
    history_table.add_column("PR")
    history_table.add_column("Reviewer")
    history_table.add_column("Decision")
    history_table.add_column("State")
    history_table.add_column("Summary", overflow="fold")
    for row in payload["review_history"]:
        history_table.add_row(
            f"#{row['pr_number']} {row['pr_title']}",
            row["reviewer"],
            row["decision_type"],
            row["thread_state"],
            row["summary"],
        )

    sections: list[RenderableType] = [
        metrics,
        Text(""),
        symbol_table if payload["symbols"] else Text("No indexed symbols for this file."),
        Text(""),
        outbound_table if payload["outbound_calls"] else Text("No outgoing calls indexed."),
        Text(""),
        inbound_table if payload["inbound_calls"] else Text("No inbound calls indexed."),
        Text(""),
        history_table if payload["review_history"] else Text("No historical review context found."),
    ]
    if payload.get("explain"):
        explain_text = Text(
            "Returned because:\n"
            + "\n".join(f"- {line}" for line in payload["retrieval_explanations"])
        )
        sections.extend(
            [Text(""), Panel(explain_text, title="Retrieval explanation", border_style="magenta")]
        )
    content = Group(*sections)
    return Panel(content, title="File context", border_style="cyan")


def render_merge_readiness(payload: dict) -> Panel:
    """Render a merge-readiness summary and action plan for a PR."""
    metrics = Table.grid(padding=(0, 2))
    metrics.add_row("Repo", payload["repo"])
    metrics.add_row("PR", f"#{payload['pr_number']} {payload['title']}")
    metrics.add_row("Author", payload["author"] or "unknown")
    metrics.add_row("State", payload["state"])
    metrics.add_row("Health score", str(payload["health_score"]))
    metrics.add_row("Readiness score", str(payload["merge_readiness_score"]))
    metrics.add_row("Assessment", payload["readiness_label"])
    metrics.add_row("Unresolved threads", str(payload["unresolved_threads"]))
    metrics.add_row("Blocking threads", str(payload["blocking_threads"]))
    metrics.add_row("Waiting on author", str(payload["waiting_on_author_threads"]))
    metrics.add_row("Waiting on reviewer", str(payload["waiting_on_reviewer_threads"]))

    actions = Text("\n".join(f"- {item}" for item in payload["recommended_actions"]))
    references = Text(
        "\n".join(f"- {item}" for item in payload["linked_references"])
        if payload["linked_references"]
        else "- none"
    )
    content = Group(
        metrics,
        Text(""),
        Text(payload["summary"], style="bold"),
        Text(""),
        Text("Recommended actions", style="bold"),
        actions,
        Text(""),
        Text("Linked references", style="bold"),
        references,
    )
    border_style = "green" if payload["blocking_threads"] == 0 else "yellow"
    return Panel(content, title="Merge readiness", border_style=border_style)


def render_retrieval_doctor(report: dict) -> Panel:
    """Render retrieval-quality diagnostics for local branch and index state."""
    metrics = Table.grid(padding=(0, 2))
    metrics.add_row("Healthy", "yes" if report["healthy"] else "no")
    metrics.add_row("Stale branch links", str(len(report["stale_branch_links"])))
    metrics.add_row("Missing code indexes", str(len(report["missing_code_indexes"])))
    metrics.add_row("Outdated indexes", str(len(report["outdated_code_indexes"])))
    metrics.add_row("Orphaned file references", str(len(report["orphaned_file_references"])))

    details = Text()
    for key, label in [
        ("stale_branch_links", "Stale branch links"),
        ("missing_code_indexes", "Missing code indexes"),
        ("outdated_code_indexes", "Outdated code indexes"),
        ("orphaned_file_references", "Orphaned file references"),
    ]:
        items = report[key]
        details.append(f"{label}:\n", style="bold")
        if not items:
            details.append("- none\n")
            continue
        for item in items:
            details.append(f"- {item}\n")
    return Panel(Group(metrics, Text(""), details), title="Retrieval doctor", border_style="cyan")
