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
body {
  font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
  margin: 0;
  background: #0b1020;
  color: #e5e7eb;
}
header, main {
  max-width: 1100px;
  margin: 0 auto;
  padding: 24px;
}
header {
  padding-bottom: 8px;
}
a {
  color: #93c5fd;
  text-decoration: none;
}
a:hover {
  text-decoration: underline;
}
.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
  margin: 16px 0 24px;
}
.card {
  background: #111827;
  border: 1px solid #1f2937;
  border-radius: 12px;
  padding: 16px;
}
.metric {
  font-size: 1.8rem;
  font-weight: 700;
  margin-top: 6px;
}
section {
  margin: 24px 0;
}
table {
  width: 100%;
  border-collapse: collapse;
  background: #111827;
  border-radius: 12px;
  overflow: hidden;
}
th, td {
  text-align: left;
  padding: 10px 12px;
  border-bottom: 1px solid #1f2937;
  vertical-align: top;
}
th {
  background: #0f172a;
}
code {
  background: #111827;
  padding: 2px 6px;
  border-radius: 6px;
}
pre {
  white-space: pre-wrap;
  background: #111827;
  border: 1px solid #1f2937;
  border-radius: 12px;
  padding: 16px;
}
form.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: end;
  margin: 12px 0 24px;
}
label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 0.95rem;
}
input {
  background: #111827;
  color: #e5e7eb;
  border: 1px solid #374151;
  border-radius: 8px;
  padding: 8px 10px;
}
button {
  background: #2563eb;
  color: white;
  border: 0;
  border-radius: 8px;
  padding: 10px 14px;
  cursor: pointer;
}
.muted {
  color: #94a3b8;
}
.error {
  color: #fca5a5;
}
"""


def _page(title: str, body: str) -> str:
    """Wrap a page body in a consistent HTML shell."""
    nav = (
        '<nav><a href="/">Dashboard</a> · <a href="/code/search">Code search</a> '
        '<span class="muted">· local-only UI backed by SQLite</span></nav>'
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


def _code_file_link(repo: str, file_path: str, label: str | None = None) -> str:
    """Render an internal indexed file-context link."""
    owner, name = repo.split("/", maxsplit=1)
    text = label or file_path
    return (
        f'<a href="/repo/{quote(owner)}/{quote(name)}/file?path={quote(file_path)}">'
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
    if repo:
        owner, name = repo.split("/", maxsplit=1)
        repo_issues_link = (
            "<section><p>"
            f"<a href='/repo/{quote(owner)}/{quote(name)}/issues'>Browse repository issues</a>"
            "</p></section>"
        )

    return "".join(
        [
            _dashboard_filters(repo, reviewer, label, stale_days),
            "".join(scope_lines),
            f'<div class="card-grid">{cards}</div>',
            repo_issues_link,
            "<section><h2>Tracked repositories</h2>",
            _table(
                ["Repo", "Open PRs", "Open issues", "Unresolved", "Blocking", "Last synced"],
                repo_rows,
            ),
            "</section>",
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
            _code_file_link(row["repo"], row["file_path"]),
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
            _code_file_link(row["repo"], row["file_path"]),
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
                    self._send_html(_page(title, body))
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
                    self._send_html(_page(title, body))
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
                    self._send_html(_page(title, body))
                    return

                if len(parts) == 4 and parts[0] == "repo" and parts[3] == "file":
                    repo = f"{parts[1]}/{parts[2]}"
                    file_path = query.get("path", [None])[0] or None
                    if not file_path:
                        raise ValueError("A file path is required via ?path=...")
                    title = f"File context · {repo}"
                    body = _render_file_context_body(
                        get_combined_file_context(
                            connection,
                            file_path=file_path,
                            repo=repo,
                        )
                    )
                    self._send_html(_page(title, body))
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
                    self._send_html(_page(title, body))
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
                    self._send_html(_page(title, _render_merge_readiness_body(payload)))
                    return

                if len(parts) == 4 and parts[0] == "pr":
                    repo = f"{parts[1]}/{parts[2]}"
                    pr_number = int(parts[3])
                    payload = get_pr_context_payload(connection, repo=repo, pr_number=pr_number)
                    title = f"PR #{pr_number} · {repo}"
                    self._send_html(_page(title, _render_pr_body(payload)))
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
                    self._send_html(_page(title, _render_issue_body(payload)))
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
