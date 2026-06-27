"""Git hook installation helpers for oss-context.

This module installs lightweight warning-only git hooks for Phase 4 workflows.
The hooks do not block pushes or commits; they surface unresolved blocking PR
review state for the current branch when oss-context is available.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

HOOK_MARKER = "# installed by oss-context"
PRE_PUSH_SCRIPT = f"""#!/bin/sh
{HOOK_MARKER}
if command -v oss-context >/dev/null 2>&1; then
  oss-context branch context --fail-on-blocking --quiet >/dev/null 2>&1
  status=$?
  if [ "$status" -eq 10 ]; then
    printf '%s\n' 'oss-context: current branch PR still has blocking review threads.'
  fi
fi
exit 0
"""
POST_COMMIT_SCRIPT = f"""#!/bin/sh
{HOOK_MARKER}
if command -v oss-context >/dev/null 2>&1; then
  oss-context branch context --fail-on-blocking --quiet >/dev/null 2>&1
  status=$?
  if [ "$status" -eq 10 ]; then
    printf '%s\n' 'oss-context: note: current branch PR still has blocking review threads.'
  fi
fi
exit 0
"""


class HookInstallError(RuntimeError):
    """Raised when hooks cannot be installed safely."""


def _write_hook(path: Path, script: str) -> None:
    """Install or update an oss-context-managed hook without clobbering user hooks."""
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if HOOK_MARKER not in existing:
            raise HookInstallError(
                f"Refusing to overwrite existing hook at {path}. Please merge it manually."
            )
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


def install_git_hooks(repo_root: Path) -> list[Path]:
    """Install warning-only post-commit and pre-push hooks into a git repo."""
    completed = subprocess.run(
        ["git", "rev-parse", "--git-path", "hooks"],
        cwd=str(repo_root),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        stderr = (
            completed.stderr.strip() or completed.stdout.strip() or "git hook path lookup failed"
        )
        raise HookInstallError(stderr)
    hooks_dir = (repo_root / completed.stdout.strip()).resolve()
    hooks_dir.mkdir(parents=True, exist_ok=True)
    pre_push_path = hooks_dir / "pre-push"
    post_commit_path = hooks_dir / "post-commit"
    _write_hook(pre_push_path, PRE_PUSH_SCRIPT)
    _write_hook(post_commit_path, POST_COMMIT_SCRIPT)
    return [pre_push_path, post_commit_path]
