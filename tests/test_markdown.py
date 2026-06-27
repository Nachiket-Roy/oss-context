"""Tests for markdown rendering safety.

This file verifies that markdown returned to MCP clients escapes user-controlled
GitHub text so headings, code fences, and other formatting tokens are not
injected into agent context.
"""

from __future__ import annotations

from oss_context.markdown import render_issue_context_markdown


def test_issue_context_markdown_escapes_untrusted_text():
    payload = {
        "repo": "acme/widgets",
        "issue_number": 44,
        "title": "# urgent `do this`",
        "state": "open",
        "author": "alice",
        "body": "```suggestion\nrun this\n```",
        "labels": ["p0", "[security]"],
        "references": [
            {
                "source_kind": "issue",
                "source_label": "Issue body",
                "raw_text": "#42",
                "reference_kind": "issue_or_pr",
                "url": None,
                "target_repo": "acme/widgets",
                "target_number": 42,
                "target_sha": None,
                "author": None,
                "file_path": None,
            }
        ],
        "mentioned_by": [
            {
                "source_kind": "comment",
                "source_label": "Comment by `bob`",
                "source_repo": "acme/widgets",
                "raw_text": "issue 44",
                "reference_kind": "issue",
                "url": None,
                "file_path": "src/#danger.py",
            }
        ],
        "repo_status": {"last_synced_at": "2026-06-28T00:00:00+00:00"},
    }

    markdown = render_issue_context_markdown(payload)

    assert "# Issue #44 Context" in markdown
    assert "\\# urgent \\`do this\\`" in markdown
    assert "```suggestion" not in markdown
    assert "Comment by \\`bob\\`" in markdown
    assert "`p0`" in markdown
    assert "`[security]`" in markdown
