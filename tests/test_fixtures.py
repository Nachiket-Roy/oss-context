"""Fixture repository tests using actual Git worktree configurations."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from oss_context.branch_context import (
    get_git_repo_root,
    get_git_worktree,
    parse_github_remote,
)


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    """Create a temporary git repository with a commit and remote configured."""
    repo_dir = tmp_path / "fixture_repo"
    repo_dir.mkdir()

    # Initialize a new git repository
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "config", "user.email", "t@ex.com"], cwd=str(repo_dir), check=True)

    # Add origin remote
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/acme/widgets.git"],
        cwd=str(repo_dir),
        check=True,
    )

    # Commit dummy file to define the main branch
    dummy = repo_dir / "dummy.py"
    dummy.write_text("print('hello')", encoding="utf-8")
    subprocess.run(["git", "add", "dummy.py"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "commit", "-m", "first commit"], cwd=str(repo_dir), check=True)

    return repo_dir


def test_parse_github_remote_variations():
    """Verify parse_github_remote handles various remote URL styles."""
    assert parse_github_remote("https://github.com/acme/widgets.git") == "acme/widgets"
    assert parse_github_remote("git@github.com:acme/widgets.git") == "acme/widgets"
    assert parse_github_remote("https://github.com/acme/widgets") == "acme/widgets"
    assert parse_github_remote("https://not-github.com/acme/widgets.git") is None


def test_git_repo_root_resolution(temp_git_repo: Path):
    """Verify get_git_repo_root accurately finds the toplevel directory."""
    sub_dir = temp_git_repo / "sub" / "folder"
    sub_dir.mkdir(parents=True)
    resolved = get_git_repo_root(sub_dir)
    assert resolved.resolve() == temp_git_repo.resolve()


def test_git_worktree_properties(temp_git_repo: Path):
    """Verify get_git_worktree inspects correct branch and remote info."""
    wt = get_git_worktree(temp_git_repo)
    assert wt["repo_root"].resolve() == temp_git_repo.resolve()
    assert wt["branch"] == "main"
    assert wt["remote_url"] == "https://github.com/acme/widgets.git"
    assert wt["repo"] == "acme/widgets"
