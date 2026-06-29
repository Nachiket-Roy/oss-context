"""Local HTML UI for oss-context.

This module serves a minimal local-only dashboard and detail pages backed by the
SQLite knowledge graph. It is intended for private, on-machine inspection of
tracked repositories, PR context, issue context, and unresolved review state.
"""

from __future__ import annotations

from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from oss_context.code_index import get_combined_file_context, search_symbols
from oss_context.db import DatabaseManager
from oss_context.queries import (
    get_dashboard_summary,
    get_issue_context_payload,
    get_pr_context_payload,
    list_repo_issues,
    list_unresolved_threads,
)
from oss_context.review_assistant import get_merge_readiness_payload
from oss_context.settings import Settings

CSS = """


:root {
  --bg-color: #080a0f;
  --surface-color: #10141f;
  --surface-hover: #151a29;
  --border-color: #1f273b;
  --border-focus: #3b82f6;
  --text-primary: #f3f4f6;
  --text-muted: #9ca3af;
  --blue: #3b82f6;
  --blue-glow: rgba(59, 130, 246, 0.15);
  --green: #10b981;
  --red: #ef4444;
  --yellow: #f59e0b;
}

body {
  font-family: 'Inter', system-ui, -apple-system, sans-serif;
  margin: 0;
  background: var(--bg-color);
  background-image: radial-gradient(circle at 50% 0%, #151d30 0%, #080a0f 70%);
  color: var(--text-primary);
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}

header, main {
  max-width: 1400px;
  width: 92%;
  margin: 0 auto;
  padding: 32px 0;
}

header {
  padding-bottom: 16px;
  border-bottom: 1px solid var(--border-color);
  display: flex;
  justify-content: space-between;
  align-items: center;
}

h1 {
  font-size: 1.75rem;
  font-weight: 700;
  margin: 0;
  background: linear-gradient(135deg, #fff 0%, #a5b4fc 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
}

a {
  color: var(--blue);
  text-decoration: none;
  transition: all 0.2s ease;
}

a:hover {
  color: #60a5fa;
  text-shadow: 0 0 8px rgba(96, 165, 250, 0.3);
}

.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 20px;
  margin: 24px 0 36px;
}

.card {
  background: var(--surface-color);
  border: 1px solid var(--border-color);
  border-radius: 12px;
  padding: 24px;
  position: relative;
  overflow: hidden;
  transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
  box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
}

.card:hover {
  transform: translateY(-4px);
  border-color: var(--blue);
  box-shadow: 0 10px 20px -5px rgba(0, 0, 0, 0.3), 0 0 15px rgba(59, 130, 246, 0.2);
}

.card .muted {
  font-size: 0.8rem;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.metric {
  font-size: 2.25rem;
  font-weight: 700;
  margin-top: 12px;
  color: #ffffff;
  letter-spacing: -0.02em;
}

.metric.blue { color: var(--blue); text-shadow: 0 0 12px rgba(59, 130, 246, 0.3); }
.metric.green { color: var(--green); text-shadow: 0 0 12px rgba(16, 185, 129, 0.3); }
.metric.red { color: var(--red); text-shadow: 0 0 12px rgba(239, 68, 68, 0.3); }

.progress-container {
  height: 6px;
  background: #1b2030;
  border-radius: 3px;
  margin-top: 16px;
  overflow: hidden;
}

.progress-bar {
  height: 100%;
  border-radius: 3px;
}

.progress-bar.green {
  background: linear-gradient(90deg, var(--green), #34d399);
  box-shadow: 0 0 8px rgba(16, 185, 129, 0.5);
}

section {
  margin: 36px 0;
}

h2 {
  font-size: 1.25rem;
  font-weight: 600;
  margin-bottom: 16px;
  color: #f3f4f6;
  border-left: 3px solid var(--blue);
  padding-left: 12px;
}

table {
  width: 100%;
  border-collapse: separate;
  border-spacing: 0;
  background: var(--surface-color);
  border: 1px solid var(--border-color);
  border-radius: 12px;
  overflow: hidden;
  box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
}

th, td {
  text-align: left;
  padding: 14px 20px;
  border-bottom: 1px solid var(--border-color);
  vertical-align: middle;
}

tr:last-child td {
  border-bottom: none;
}

th {
  background: #0b0e14;
  color: var(--text-muted);
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  border-bottom: 1px solid var(--border-color);
}

tr {
  transition: background 0.15s ease;
}

tr:hover td {
  background: var(--surface-hover);
}

code {
  background: rgba(255, 255, 255, 0.06);
  padding: 3px 8px;
  border-radius: 6px;
  font-family: 'Fira Code', 'Courier New', Courier, monospace;
  font-size: 0.85rem;
  color: #e5e7eb;
}

pre {
  white-space: pre-wrap;
  background: var(--surface-color);
  border: 1px solid var(--border-color);
  border-radius: 12px;
  padding: 20px;
  color: #e5e7eb;
  font-family: 'Fira Code', monospace;
  font-size: 0.9rem;
}

form.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  align-items: flex-end;
  background: var(--surface-color);
  border: 1px solid var(--border-color);
  border-radius: 12px;
  padding: 20px;
  margin: 16px 0 32px;
}

label {
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-size: 0.8rem;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

input {
  background: #0c0e14;
  color: var(--text-primary);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  padding: 10px 16px;
  font-size: 0.9rem;
  transition: all 0.2s ease;
}

input:focus {
  outline: none;
  border-color: var(--blue);
  box-shadow: 0 0 0 3px var(--blue-glow);
  background: #10141f;
}

button {
  background: linear-gradient(135deg, var(--blue) 0%, #1d4ed8 100%);
  color: white;
  border: 0;
  border-radius: 8px;
  padding: 12px 24px;
  cursor: pointer;
  font-size: 0.9rem;
  font-weight: 600;
  transition: all 0.2s ease;
  box-shadow: 0 4px 6px rgba(59, 130, 246, 0.25);
}

button:hover {
  transform: translateY(-1px);
  box-shadow: 0 6px 12px rgba(59, 130, 246, 0.35);
  opacity: 0.95;
}

button:active {
  transform: translateY(1px);
}

.muted {
  color: var(--text-muted);
}

.error {
  color: #f87171;
  background: rgba(239, 68, 68, 0.1);
  border: 1px solid rgba(239, 68, 68, 0.2);
  border-radius: 8px;
  padding: 12px 16px;
}

nav {
  display: flex;
  gap: 8px;
}

nav a {
  color: var(--text-muted);
  text-decoration: none;
  padding: 8px 16px;
  border-radius: 8px;
  font-size: 0.9rem;
  font-weight: 500;
  transition: all 0.2s ease;
}

nav a:hover {
  color: #fff;
  background: rgba(255, 255, 255, 0.05);
  text-decoration: none;
}

nav a.active {
  color: #fff;
  background: var(--blue);
  box-shadow: 0 4px 6px rgba(59, 130, 246, 0.2);
  text-decoration: none;
}
"""


def _page(title: str, body: str, active_tab: str = "dashboard") -> str:
    """Wrap a page body in a consistent HTML shell."""
    is_db = "active" if active_tab == "dashboard" else ""
    is_sr = "active" if active_tab == "search" else ""
    nav = (
        f'<nav>'
        f'<a href="/" class="{is_db}">Dashboard</a>'
        f'<a href="/code/search" class="{is_sr}">Code search</a>'
        f'</nav>'
    )
    return (
        "<!doctype html>"
        "<html><head>"
        f"<meta charset='utf-8'><title>{escape(title)}</title>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<style>{CSS}</style>"
        "</head><body>"
        f"<header><h1>{escape(title)}</h1>{nav}</header>"
        f"<main>{body}</main>"
        "</body></html>"
    )


def _card(label: str, value: str) -> str:
    """Render a simple metric card."""
    return (
        '<div class="card">'
        f"<div class='muted'>{escape(label)}</div>"
        f"<div class='metric'>{escape(value)}</div>"
        "</div>"
    )


def _table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a simple HTML table."""
    if not rows:
        return '<p class="muted">No data.</p>'

    header_html = "".join(f"<th>{escape(header)}</th>" for header in headers)
    row_html = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows
    )
    return f"<table><thead><tr>{header_html}</tr></thead><tbody>{row_html}</tbody></table>"


def _repo_link(repo: str) -> str:
    """Render an internal repo link."""
    owner, name = repo.split("/", maxsplit=1)
    return f'<a href="/repo/{quote(owner)}/{quote(name)}">{escape(repo)}</a>'


def _pr_link(repo: str, pr_number: int, label: str | None = None) -> str:
    """Render an internal PR detail link."""
    owner, name = repo.split("/", maxsplit=1)
    text = label or f"{repo}#{pr_number}"
    return f'<a href="/pr/{quote(owner)}/{quote(name)}/{pr_number}">{escape(text)}</a>'


def _issue_link(repo: str, issue_number: int, label: str | None = None) -> str:
    """Render an internal issue detail link."""
    owner, name = repo.split("/", maxsplit=1)
    text = label or f"{repo}#{issue_number}"
    return f'<a href="/issue/{quote(owner)}/{quote(name)}/{issue_number}">{escape(text)}</a>'


def _code_file_link(
    repo: str,
    file_path: str,
    label: str | None = None,
    branch: str | None = None,
) -> str:
    """Render an internal indexed file-context link."""
    text = label or file_path
    if "/" not in repo or repo.startswith("/") or repo.count("/") != 1:
        return escape(text)
    owner, name = repo.split("/", maxsplit=1)
    href = f"/repo/{quote(owner)}/{quote(name)}/file?path={quote(file_path)}"
    if branch:
        href += f"&branch={quote(branch)}"
    return (
        f'<a href="{href}">'
        f"{escape(text)}"
        "</a>"
    )


def _safe_external_link(url: str) -> str | None:
    """Return a safe external href for rendered references."""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    return url


def _reference_target(reference: dict[str, Any]) -> str:
    """Render a linked or plain-text reference target."""
    repo = reference.get("target_repo")
    number = reference.get("target_number")
    kind = reference.get("reference_kind")
    if kind == "discussion":
        url = reference.get("url")
        title_suffix = f" ({reference['title']})" if reference.get("title") else ""
        text = f"Discussion #{number}{title_suffix}"
        if url:
            safe_url = _safe_external_link(url)
            if safe_url is not None:
                return (
                    f'<a href="{escape(safe_url, quote=True)}" target="_blank" '
                    f'rel="noopener noreferrer">'
                    f"{escape(text)}</a>"
                )
        return f"<code>{escape(text)}</code>"
    if repo and number is not None and kind == "pull_request":
        return _pr_link(repo, number)
    if repo and number is not None and kind == "issue":
        return _issue_link(repo, number)
    if reference.get("url"):
        url = str(reference["url"])
        safe_url = _safe_external_link(url)
        if safe_url is not None:
            return f'<a href="{escape(safe_url, quote=True)}">{escape(safe_url)}</a>'
        return f"<code>{escape(url)}</code>"
    if repo and number is not None:
        return f"<code>{escape(repo)}#{number}</code>"
    if repo and reference.get("target_sha"):
        return f"<code>{escape(repo)}@{escape(str(reference['target_sha']))}</code>"
    return f"<code>{escape(str(reference.get('raw_text') or ''))}</code>"


def _dashboard_filters(
    repo: str | None, reviewer: str | None, label: str | None, stale: int
) -> str:
    """Render the dashboard filter form."""
    repo_value = repo or ""
    reviewer_value = reviewer or ""
    label_value = label or ""
    return (
        '<form class="filters" method="get">'
        f"<label>Repo<input name='repo' value='{escape(repo_value, quote=True)}' "
        "placeholder='owner/name'></label>"
        f"<label>Reviewer<input name='reviewer' value='{escape(reviewer_value, quote=True)}' "
        "placeholder='alice'></label>"
        f"<label>Label<input name='label' value='{escape(label_value, quote=True)}' "
        "placeholder='security'></label>"
        f"<label>Stale days<input name='stale' type='number' min='0' value='{stale}'></label>"
        "<button type='submit'>Apply</button>"
        "</form>"
    )


def _render_issue_list_body(
    *,
    connection,
    repo: str,
    state: str | None,
    label: str | None,
) -> str:
    """Render a repo-scoped issue list page."""
    issues = list_repo_issues(connection, repo=repo, state=state, label=label, limit=200)
    issue_rows = [
        [
            _issue_link(
                issue["repo"], issue["issue_number"], f"#{issue['issue_number']} {issue['title']}"
            ),
            escape(issue["state"]),
            escape(issue["author"]),
            escape(", ".join(issue["labels"]) if issue["labels"] else "—"),
            escape(issue["summary"] or "—"),
        ]
        for issue in issues
    ]
    filters = (
        '<form class="filters" method="get">'
        f"<label>State<input name='state' value='{escape(state or '', quote=True)}' "
        "placeholder='open'></label>"
        f"<label>Label<input name='label' value='{escape(label or '', quote=True)}' "
        "placeholder='bug'></label>"
        "<button type='submit'>Apply</button>"
        "</form>"
    )
    return "".join(
        [
            f"<p><strong>Repository:</strong> {_repo_link(repo)}</p>",
            filters,
            "<section><h2>Issues</h2>",
            _table(["Issue", "State", "Author", "Labels", "Summary"], issue_rows),
            "</section>",
        ]
    )


def _render_dashboard_body(
    *,
    connection,
    repo: str | None,
    reviewer: str | None,
    label: str | None,
    stale_days: int,
) -> str:
    """Render the dashboard or repo overview page body."""
    summary = get_dashboard_summary(
        connection,
        repo=repo,
        reviewer=reviewer,
        label=label,
        stale_days=stale_days,
    )
    unresolved = list_unresolved_threads(
        connection,
        repo=repo,
        author=reviewer,
        label=label,
        stale_days=stale_days if stale_days >= 0 else None,
    )

    cards = "".join(
        [
            _card("Repos tracked", str(summary["repos_tracked"])),
            _card("Open PRs", str(summary["open_prs"])),
            _card("Unresolved threads", str(summary["unresolved_threads"])),
            _card("Blocking threads", str(summary["blocking_threads"])),
            _card(f"Stale threads (≥{stale_days}d)", str(summary["stale_threads"])),
        ]
    )

    repo_rows = [
        [
            _repo_link(row["repo"]),
            escape(str(row["open_prs"])),
            escape(str(row.get("open_issues", 0))),
            escape(str(row["unresolved_threads"])),
            escape(str(row["blocking_threads"])),
            escape(row["last_synced_at"] or "never"),
        ]
        for row in summary["repo_breakdown"]
    ]
    unresolved_rows = [
        [
            _pr_link(row["repo"], row["pr_number"], f"#{row['pr_number']} {row['pr_title']}"),
            f"<code>{escape(row['file_path'])}</code>",
            escape(row["reviewer"]),
            escape(row["decision_type"]),
            escape(row["waiting_on"]),
            escape(row["summary"]),
        ]
        for row in unresolved[:50]
    ]

    scope_lines = [
        f"<p><strong>Repo scope:</strong> {escape(summary['repo'] or 'all repos')}</p>",
        f"<p><strong>Reviewer scope:</strong> {escape(summary['reviewer'] or 'all reviewers')}</p>",
        f"<p><strong>Label scope:</strong> {escape(summary.get('label') or 'all labels')}</p>",
    ]

    repo_issues_link = ""
    issues_section = ""
    if repo:
        try:
            owner, name = repo.split("/", maxsplit=1)
        except ValueError:
            return _page(
                "Error",
                "<p class='error'>Invalid repository format. Use owner/name.</p>"
            )
        repo_issues_link = (
            "<section><p>"
            f"<a href='/repo/{quote(owner)}/{quote(name)}/issues'>Browse repository issues</a>"
            "</p></section>"
        )
        issue_list = list_repo_issues(connection, repo=repo, limit=50)
        issue_rows = [
            [
                _issue_link(repo, row["issue_number"], f"#{row['issue_number']} {row['title']}"),
                escape(row["state"]),
                escape(row["author"] or "unknown"),
                escape(row["updated_at"] or ""),
            ]
            for row in issue_list
        ]
        issues_section = (
            "<section><h2>Repository Issues</h2>"
            + _table(["Issue", "State", "Author", "Last Updated"], issue_rows)
            + "</section>"
        )

    return "".join(
        [
            _dashboard_filters(repo, reviewer, label, stale_days),
            "".join(scope_lines),
            f'<div class="card-grid">{cards}</div>',
            repo_issues_link,
            "" if repo else "<section><h2>Tracked repositories</h2>",
            "" if repo else _table(
                ["Repo", "Open PRs", "Open issues", "Unresolved", "Blocking", "Last synced"],
                repo_rows,
            ),
            "" if repo else "</section>",
            issues_section if repo else "",
            "<section><h2>Unresolved threads</h2>",
            _table(
                ["PR", "File", "Reviewer", "Decision", "Waiting on", "Summary"],
                unresolved_rows,
            ),
            "</section>",
        ]
    )


def _render_pr_body(payload: dict[str, Any]) -> str:
    """Render a PR detail page."""
    health = payload["health"]
    cards = "".join(
        [
            _card("Health score", str(health["health_score"])),
            _card("Unresolved threads", str(health["unresolved_threads"])),
            _card("Blocking threads", str(health["blocking_threads"])),
            _card("Approvals", str(health["approvals"])),
        ]
    )
    reference_rows = [
        [
            escape(row["source_label"]),
            escape(row["reference_kind"]),
            _reference_target(row),
        ]
        for row in payload["references"]
    ]
    thread_rows = [
        [
            _code_file_link(payload["repo"], row["file_path"], row["file_path"]),
            escape(row["reviewer"]),
            escape(row["decision_type"]),
            escape(row["waiting_on"]),
            escape(row["summary"]),
        ]
        for row in payload["unresolved_threads"]
    ]
    decision_rows = [
        [
            escape(row["author"]),
            escape(row["decision_type"]),
            escape(f"{row['confidence']:.2f}"),
            _code_file_link(payload["repo"], row["file_path"], row["file_path"]),
            escape(row["summary"]),
        ]
        for row in payload["decisions"]
    ]
    labels = ", ".join(payload["labels"]) if payload["labels"] else "—"
    owner, name = payload["repo"].split("/", maxsplit=1)

    last_synced = escape(payload["repo_status"]["last_synced_at"] or "never")
    return "".join(
        [
            f"<p><strong>Repo:</strong> {_repo_link(payload['repo'])}</p>",
            f"<p><strong>Labels:</strong> {escape(labels)}</p>",
            f"<p><strong>Last synced:</strong> {last_synced}</p>",
            (
                f"<p><a href='/pr/{quote(owner)}/{quote(name)}/"
                f"{payload['pr_number']}/ready'>Open merge-readiness view</a></p>"
            ),
            f'<div class="card-grid">{cards}</div>',
            "<section><h2>Linked references</h2>",
            _table(["Source", "Kind", "Target"], reference_rows),
            "</section>",
            "<section><h2>Unresolved threads</h2>",
            _table(["File", "Reviewer", "Decision", "Waiting on", "Summary"], thread_rows),
            "</section>",
            "<section><h2>Decision history</h2>",
            _table(["Author", "Decision", "Confidence", "File", "Summary"], decision_rows),
            "</section>",
        ]
    )


def _render_issue_body(payload: dict[str, Any]) -> str:
    """Render an issue detail page."""
    labels = ", ".join(payload["labels"]) if payload["labels"] else "—"
    reference_rows = [
        [
            escape(row["source_label"]),
            escape(row["reference_kind"]),
            _reference_target(row),
        ]
        for row in payload["references"]
    ]
    mention_rows = [
        [
            escape(row["source_label"]),
            _repo_link(row["source_repo"]),
            escape(row["file_path"] or "—"),
        ]
        for row in payload["mentioned_by"]
    ]
    comment_rows = [
        [
            escape(c["author"] or "unknown"),
            escape(c["created_at"] or ""),
            escape(c["body"] or ""),
        ]
        for c in payload.get("comments", [])
    ]

    last_synced = escape(payload["repo_status"]["last_synced_at"] or "never")
    return "".join(
        [
            f"<p><strong>Repo:</strong> {_repo_link(payload['repo'])}</p>",
            f"<p><strong>State:</strong> {escape(payload['state'])}</p>",
            f"<p><strong>Author:</strong> {escape(payload['author'] or 'unknown')}</p>",
            f"<p><strong>Labels:</strong> {escape(labels)}</p>",
            f"<p><strong>Last synced:</strong> {last_synced}</p>",
            "<section><h2>Body</h2>",
            f"<pre>{escape(payload['body'] or 'No issue body.')}</pre>",
            "</section>",
            "<section><h2>Outbound references</h2>",
            _table(["Source", "Kind", "Target"], reference_rows),
            "</section>",
            "<section><h2>Mentioned by</h2>",
            _table(["Mentioned by", "Repo", "File"], mention_rows),
            "</section>",
            "<section><h2>Activity (Comments)</h2>",
            _table(["Author", "Date", "Comment"], comment_rows),
            "</section>",
        ]
    )


def _render_code_search_body(
    *,
    connection,
    query: str | None,
    repo: str | None,
    branch: str | None,
) -> str:
    """Render a code-search page backed by the local SQLite index."""
    rows = (
        search_symbols(connection, query=query, repo=repo, branch=branch, limit=100)
        if query
        else []
    )
    form = (
        '<form class="filters" method="get">'
        f"<label>Symbol<input name='q' value='{escape(query or '', quote=True)}' "
        "placeholder='AuthService'></label>"
        f"<label>Repo<input name='repo' value='{escape(repo or '', quote=True)}' "
        "placeholder='owner/name'></label>"
        f"<label>Branch<input name='branch' value='{escape(branch or '', quote=True)}' "
        "placeholder='main'></label>"
        "<button type='submit'>Search</button>"
        "</form>"
    )
    result_rows = [
        [
            escape(row["repo"]),
            escape(row["branch"] or "—"),
            escape(row["kind"]),
            escape(row["qualified_name"]),
            _code_file_link(row["repo"], row["file_path"], branch=row.get("branch")),
            escape(str(row["line_number"] or "—")),
        ]
        for row in rows
        if "/" in row["repo"]
    ]
    empty = (
        '<p class="muted">Enter a symbol name to search the local code index.</p>'
        if not query
        else ""
    )
    return "".join(
        [
            form,
            empty,
            "<section><h2>Results</h2>",
            _table(["Repo", "Branch", "Kind", "Symbol", "File", "Line"], result_rows),
            "</section>",
        ]
    )


def _render_file_context_body(payload: dict[str, Any]) -> str:
    """Render an indexed file-context page."""
    symbol_rows = [
        [escape(row["kind"]), escape(row["qualified_name"]), escape(str(row["line_number"] or "—"))]
        for row in payload["symbols"]
    ]
    outbound_rows = [
        [
            escape(row["callee"]),
            escape(str(row["call_count"])),
            escape(str(row["first_line"] or "—")),
        ]
        for row in payload["outbound_calls"]
    ]
    inbound_rows = [
        [
            escape(row["repo"]),
            _code_file_link(row["repo"], row["file_path"], branch=row.get("branch")),
            escape(row["caller"]),
            escape(str(row["line_number"] or "—")),
        ]
        for row in payload["inbound_calls"]
        if "/" in row["repo"]
    ]
    history_rows = [
        [
            _pr_link(payload["repo"], row["pr_number"], f"#{row['pr_number']} {row['pr_title']}"),
            escape(row["reviewer"]),
            escape(row["decision_type"]),
            escape(row["thread_state"]),
            escape(row["summary"]),
        ]
        for row in payload["review_history"]
        if payload["repo"]
    ]
    return "".join(
        [
            f"<p><strong>Repo:</strong> {escape(payload['repo'] or 'local workspace')}</p>",
            f"<p><strong>File:</strong> <code>{escape(payload['file_path'])}</code></p>",
            f"<p><strong>Branch:</strong> {escape(payload['branch'] or 'detached/unknown')}</p>",
            f"<p><strong>Commit:</strong> {escape(payload['commit'] or 'unknown')}</p>",
            f"<p><strong>Indexed at:</strong> {escape(payload['indexed_at'])}</p>",
            "<section><h2>Defined symbols</h2>",
            _table(["Kind", "Qualified name", "Line"], symbol_rows),
            "</section>",
            "<section><h2>Outgoing calls</h2>",
            _table(["Callee", "Count", "First line"], outbound_rows),
            "</section>",
            "<section><h2>Inbound calls</h2>",
            _table(["Repo", "File", "Caller", "Line"], inbound_rows),
            "</section>",
            "<section><h2>Review history</h2>",
            _table(["PR", "Reviewer", "Decision", "State", "Summary"], history_rows),
            "</section>",
        ]
    )


def _render_merge_readiness_body(payload: dict[str, Any]) -> str:
    """Render a PR merge-readiness page."""
    cards = "".join(
        [
            _card("Health score", str(payload["health_score"])),
            _card("Readiness score", str(payload["merge_readiness_score"])),
            _card("Blocking threads", str(payload["blocking_threads"])),
            _card("Waiting on reviewer", str(payload["waiting_on_reviewer_threads"])),
        ]
    )
    actions = "".join(f"<li>{escape(item)}</li>" for item in payload["recommended_actions"])
    references = "".join(f"<li>{escape(item)}</li>" for item in payload["linked_references"])
    pr_label = f"#{payload['pr_number']} {payload['title']}"
    return "".join(
        [
            f"<p><strong>Repo:</strong> {_repo_link(payload['repo'])}</p>",
            (
                f"<p><strong>PR:</strong> "
                f"{_pr_link(payload['repo'], payload['pr_number'], pr_label)}</p>"
            ),
            f"<p><strong>Assessment:</strong> {escape(payload['readiness_label'])}</p>",
            f"<p><strong>Summary:</strong> {escape(payload['summary'])}</p>",
            f'<div class="card-grid">{cards}</div>',
            "<section><h2>Recommended actions</h2><ul>",
            actions or '<li class="muted">No immediate follow-up actions.</li>',
            "</ul></section>",
            "<section><h2>Linked references</h2><ul>",
            references or '<li class="muted">No linked references.</li>',
            "</ul></section>",
        ]
    )


def serve_web_ui(settings: Settings, *, host: str = "127.0.0.1", port: int = 8080) -> None:
    """Run the local HTML UI server."""

    class Handler(BaseHTTPRequestHandler):
        """Request handler for the local oss-context HTML UI."""

        def log_message(self, format: str, *args: object) -> None:
            return

        def _send_html(self, html: str, *, status: int = 200) -> None:
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            parts = [part for part in parsed.path.split("/") if part]

            if parsed.path == "/favicon.ico":
                self.send_response(404)
                self.end_headers()
                return

            connection = DatabaseManager(settings.db_path).initialize()
            try:
                stale_days = int(query.get("stale", ["7"])[0])
                reviewer = query.get("reviewer", [None])[0] or None
                label = query.get("label", [None])[0] or None

                if not parts:
                    repo = query.get("repo", [None])[0] or None
                    title = "oss-context dashboard"
                    body = _render_dashboard_body(
                        connection=connection,
                        repo=repo,
                        reviewer=reviewer,
                        label=label,
                        stale_days=stale_days,
                    )
                    self._send_html(_page(title, body, active_tab="dashboard"))
                    return

                if len(parts) == 2 and parts[0] == "code" and parts[1] == "search":
                    repo = query.get("repo", [None])[0] or None
                    branch = query.get("branch", [None])[0] or None
                    search_query = query.get("q", [None])[0] or None
                    title = "Code search"
                    body = _render_code_search_body(
                        connection=connection,
                        query=search_query,
                        repo=repo,
                        branch=branch,
                    )
                    self._send_html(_page(title, body, active_tab="search"))
                    return

                if len(parts) == 4 and parts[0] == "repo" and parts[3] == "issues":
                    repo = f"{parts[1]}/{parts[2]}"
                    state = query.get("state", [None])[0] or None
                    title = f"Issues · {repo}"
                    body = _render_issue_list_body(
                        connection=connection,
                        repo=repo,
                        state=state,
                        label=label,
                    )
                    self._send_html(_page(title, body, active_tab="dashboard"))
                    return

                if len(parts) == 4 and parts[0] == "repo" and parts[3] == "file":
                    repo = f"{parts[1]}/{parts[2]}"
                    file_path = query.get("path", [None])[0] or None
                    branch = query.get("branch", [None])[0] or None
                    if not file_path:
                        raise ValueError("A file path is required via ?path=...")
                    title = f"File context · {repo}"
                    body = _render_file_context_body(
                        get_combined_file_context(
                            connection,
                            file_path=file_path,
                            repo=repo,
                            branch=branch,
                        )
                    )
                    self._send_html(_page(title, body, active_tab="dashboard"))
                    return

                if len(parts) == 3 and parts[0] == "repo":
                    repo = f"{parts[1]}/{parts[2]}"
                    title = f"Repository · {repo}"
                    body = _render_dashboard_body(
                        connection=connection,
                        repo=repo,
                        reviewer=reviewer,
                        label=label,
                        stale_days=stale_days,
                    )
                    self._send_html(_page(title, body, active_tab="dashboard"))
                    return

                if len(parts) == 5 and parts[0] == "pr" and parts[4] == "ready":
                    repo = f"{parts[1]}/{parts[2]}"
                    pr_number = int(parts[3])
                    payload = get_merge_readiness_payload(
                        connection,
                        repo=repo,
                        pr_number=pr_number,
                        stale_days=stale_days,
                    )
                    title = f"Merge readiness · PR #{pr_number} · {repo}"
                    self._send_html(
                        _page(
                            title,
                            _render_merge_readiness_body(payload),
                            active_tab="dashboard",
                        )
                    )
                    return

                if len(parts) == 4 and parts[0] == "pr":
                    repo = f"{parts[1]}/{parts[2]}"
                    pr_number = int(parts[3])
                    payload = get_pr_context_payload(
                        connection, repo=repo, pr_number=pr_number
                    )
                    title = f"PR #{pr_number} · {repo}"
                    self._send_html(
                        _page(
                            title,
                            _render_pr_body(payload),
                            active_tab="dashboard",
                        )
                    )
                    return

                if len(parts) == 4 and parts[0] == "issue":
                    repo = f"{parts[1]}/{parts[2]}"
                    issue_number = int(parts[3])
                    payload = get_issue_context_payload(
                        connection,
                        repo=repo,
                        issue_number=issue_number,
                    )
                    title = f"Issue #{issue_number} · {repo}"
                    self._send_html(
                        _page(
                            title,
                            _render_issue_body(payload),
                            active_tab="dashboard",
                        )
                    )
                    return

                self._send_html(
                    _page("Not found", '<p class="error">Route not found.</p>'), status=404
                )
            except ValueError as exc:
                body = f'<p class="error">{escape(str(exc))}</p>'
                self._send_html(_page("Error", body), status=404)
            finally:
                connection.close()

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"oss-context UI available at http://{host}:{port}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
