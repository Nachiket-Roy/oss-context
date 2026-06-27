"""Tests for deterministic reference extraction.

This file verifies that GitHub URLs, shorthand references, and generic links
are normalized into structured reference records before being stored in SQLite.
"""

from __future__ import annotations

from oss_context.references import extract_references


def test_extract_references_parses_issue_pr_commit_and_urls():
    text = (
        "See https://github.com/acme/widgets/pull/42, "
        "https://github.com/acme/widgets/issues/44, "
        "https://github.com/acme/widgets/commit/abcdef1234, "
        "acme/gadgets#18, issue 44, #51, and https://example.com/docs."
    )

    references = extract_references(text, repo="acme/widgets")

    assert ("pull_request", "acme/widgets", 42) in {
        (ref.kind, ref.target_repo, ref.target_number) for ref in references
    }
    assert ("issue", "acme/widgets", 44) in {
        (ref.kind, ref.target_repo, ref.target_number) for ref in references
    }
    assert any(ref.kind == "commit" and ref.target_sha == "abcdef1234" for ref in references)
    assert any(
        ref.kind == "issue_or_pr" and ref.target_repo == "acme/gadgets" and ref.target_number == 18
        for ref in references
    )
    assert any(
        ref.kind == "issue_or_pr" and ref.target_repo == "acme/widgets" and ref.target_number == 51
        for ref in references
    )
    assert any(ref.kind == "url" and ref.url == "https://example.com/docs." for ref in references)
