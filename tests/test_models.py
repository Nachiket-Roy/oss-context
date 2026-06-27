"""Tests for shared model validation helpers.

This file focuses on repository slug parsing so malformed CLI and query inputs
are rejected before they reach deeper sync or database code.
"""

import pytest

from oss_context.models import RepoRef


@pytest.mark.parametrize(
    "value",
    [
        "owner",
        "owner/repo/extra",
        "/repo",
        "owner/",
        "owner//repo",
    ],
)
def test_repo_ref_from_slug_rejects_malformed_values(value):
    with pytest.raises(ValueError, match="owner/name"):
        RepoRef.from_slug(value)


def test_repo_ref_from_slug_normalizes_whitespace():
    repo = RepoRef.from_slug("  owner / repo  ")
    assert repo.owner == "owner"
    assert repo.name == "repo"
    assert repo.slug == "owner/repo"
