"""Deterministic reference extraction for PRs, issues, comments, and links.

This module parses GitHub URLs, shorthand references, explicit issue mentions,
commit links, and generic URLs into normalized reference records that can be
stored and queried from SQLite.
"""

from __future__ import annotations

import re

from oss_context.models import ExtractedReference, RepoRef

GITHUB_PULL_URL_RE = re.compile(
    r"https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<number>\d+)"
)
GITHUB_ISSUE_URL_RE = re.compile(
    r"https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/issues/(?P<number>\d+)"
)
GITHUB_COMMIT_URL_RE = re.compile(
    r"https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/commit/(?P<sha>[0-9a-fA-F]{7,40})"
)
CROSS_REPO_REF_RE = re.compile(
    r"(?<![A-Za-z0-9_.\-/])(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)#(?P<number>\d+)"
)
EXPLICIT_ISSUE_RE = re.compile(r"\bissue\s+#?(?P<number>\d+)\b", re.IGNORECASE)
BARE_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9_/.-])#(?P<number>\d+)\b")
URL_RE = re.compile(r"https?://[^\s)>\]]+")


def _span_overlaps(existing: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(
        not (end <= other_start or start >= other_end) for other_start, other_end in existing
    )


def extract_references(text: str | None, *, repo: str) -> list[ExtractedReference]:
    if not text:
        return []

    repo_ref = RepoRef.from_slug(repo)
    spans: list[tuple[int, int]] = []
    results: list[ExtractedReference] = []

    def add_reference(match: re.Match[str], reference: ExtractedReference) -> None:
        start, end = match.span()
        if _span_overlaps(spans, start, end):
            return
        spans.append((start, end))
        results.append(reference)

    for match in GITHUB_PULL_URL_RE.finditer(text):
        add_reference(
            match,
            ExtractedReference(
                kind="pull_request",
                raw_text=match.group(0),
                url=match.group(0),
                target_repo=f"{match.group('owner')}/{match.group('repo')}",
                target_number=int(match.group("number")),
            ),
        )

    for match in GITHUB_ISSUE_URL_RE.finditer(text):
        add_reference(
            match,
            ExtractedReference(
                kind="issue",
                raw_text=match.group(0),
                url=match.group(0),
                target_repo=f"{match.group('owner')}/{match.group('repo')}",
                target_number=int(match.group("number")),
            ),
        )

    for match in GITHUB_COMMIT_URL_RE.finditer(text):
        add_reference(
            match,
            ExtractedReference(
                kind="commit",
                raw_text=match.group(0),
                url=match.group(0),
                target_repo=f"{match.group('owner')}/{match.group('repo')}",
                target_sha=match.group("sha"),
            ),
        )

    for match in CROSS_REPO_REF_RE.finditer(text):
        add_reference(
            match,
            ExtractedReference(
                kind="issue_or_pr",
                raw_text=match.group(0),
                target_repo=f"{match.group('owner')}/{match.group('repo')}",
                target_number=int(match.group("number")),
            ),
        )

    for match in EXPLICIT_ISSUE_RE.finditer(text):
        add_reference(
            match,
            ExtractedReference(
                kind="issue",
                raw_text=match.group(0),
                target_repo=repo_ref.slug,
                target_number=int(match.group("number")),
            ),
        )

    for match in BARE_NUMBER_RE.finditer(text):
        add_reference(
            match,
            ExtractedReference(
                kind="issue_or_pr",
                raw_text=match.group(0),
                target_repo=repo_ref.slug,
                target_number=int(match.group("number")),
            ),
        )

    for match in URL_RE.finditer(text):
        add_reference(
            match,
            ExtractedReference(
                kind="url",
                raw_text=match.group(0),
                url=match.group(0),
            ),
        )

    deduped: list[ExtractedReference] = []
    seen: set[tuple] = set()
    for reference in results:
        key = (
            reference.kind,
            reference.raw_text,
            reference.url,
            reference.target_repo,
            reference.target_number,
            reference.target_sha,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(reference)
    return deduped
