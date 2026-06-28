"""Local code indexing and retrieval helpers for oss-context.

This module builds a lightweight Python code index in SQLite so the rest of the
application can answer symbol, caller/callee, impacted-file, and file-context
questions alongside synced PR and issue review state.
"""

from __future__ import annotations

import ast
import hashlib
import logging
import sqlite3
import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oss_context.branch_context import parse_github_remote
from oss_context.models import RepoRef
from oss_context.retrieval import build_provenance, summarize_provenance

LOGGER = logging.getLogger(__name__)
COMMAND_TIMEOUT_SECONDS = 3.0
EXCLUDED_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
}


class _PythonIndexer(ast.NodeVisitor):
    """Extract Python symbol definitions and call edges from a parsed AST."""

    def __init__(self) -> None:
        self.symbols: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self._scope: list[str] = []
        self._class_depth = 0

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualified_name = ".".join([*self._scope, node.name])
        parent = ".".join(self._scope) or None
        self.symbols.append(
            {
                "name": node.name,
                "qualified_name": qualified_name,
                "kind": "class",
                "parent_qualified_name": parent,
                "lineno": node.lineno,
                "end_lineno": getattr(node, "end_lineno", node.lineno),
            }
        )
        self._scope.append(node.name)
        self._class_depth += 1
        self.generic_visit(node)
        self._class_depth -= 1
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node, kind="method" if self._class_depth else "function")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node, kind="method" if self._class_depth else "function")

    def visit_Call(self, node: ast.Call) -> None:
        callee_name = _extract_callee_name(node.func)
        if callee_name and self._scope:
            self.calls.append(
                {
                    "caller_qualified_name": ".".join(self._scope),
                    "callee_name": callee_name,
                    "lineno": node.lineno,
                }
            )
        self.generic_visit(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, *, kind: str) -> None:
        qualified_name = ".".join([*self._scope, node.name])
        parent = ".".join(self._scope) or None
        self.symbols.append(
            {
                "name": node.name,
                "qualified_name": qualified_name,
                "kind": kind,
                "parent_qualified_name": parent,
                "lineno": node.lineno,
                "end_lineno": getattr(node, "end_lineno", node.lineno),
            }
        )
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()


def _extract_callee_name(node: ast.AST) -> str | None:
    """Collapse a call target to a stable callee name for the local index."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _best_effort_command(args: list[str], cwd: Path) -> str | None:
    """Run a local command and return stripped stdout on success."""
    try:
        completed = subprocess.run(
            args,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOGGER.debug("Command failed while inspecting workspace: %s", exc)
        return None
    if completed.returncode != 0:
        return None
    output = completed.stdout.strip()
    return output or None


def _detect_workspace(cwd: Path | None = None, *, repo: str | None = None) -> dict[str, Any]:
    """Best-effort workspace detection for local indexing."""
    requested_root = (cwd or Path.cwd()).resolve()
    repo_root_raw = _best_effort_command(["git", "rev-parse", "--show-toplevel"], requested_root)
    repo_root = Path(repo_root_raw).resolve() if repo_root_raw else requested_root
    remote_url = _best_effort_command(["git", "remote", "get-url", "origin"], repo_root)
    detected_repo = repo or parse_github_remote(remote_url)
    if detected_repo is not None:
        detected_repo = RepoRef.from_slug(detected_repo).slug
    branch_name = _best_effort_command(["git", "branch", "--show-current"], repo_root)
    git_commit = _best_effort_command(["git", "rev-parse", "HEAD"], repo_root)
    return {
        "repo": detected_repo,
        "repo_root": repo_root,
        "branch": branch_name,
        "commit": git_commit,
    }


def _iter_python_files(repo_root: Path) -> Iterable[Path]:
    """Yield Python source files under the workspace while skipping common cache dirs."""
    for path in repo_root.rglob("*.py"):
        if any(part in EXCLUDED_DIRECTORIES for part in path.parts):
            continue
        if path.is_file():
            yield path


def _hash_file(path: Path) -> str:
    """Hash file contents for incremental reuse between snapshots."""
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _normalize_repo_path(path: Path, repo_root: Path) -> str:
    """Normalize a path to a repo-relative POSIX path for stable storage."""
    return path.resolve().relative_to(repo_root.resolve()).as_posix()


def _parse_python_file(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse a Python file into symbol and call records."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    indexer = _PythonIndexer()
    indexer.visit(tree)
    return indexer.symbols, indexer.calls


def _latest_snapshot_row(
    connection: sqlite3.Connection,
    *,
    repo_root: str,
    branch: str | None,
) -> sqlite3.Row | None:
    """Return the latest snapshot for a repo root and branch combination."""
    if branch is None:
        return connection.execute(
            """
            SELECT id, repo_slug, repo_root, git_branch, git_commit, indexed_at
            FROM code_index_snapshots
            WHERE repo_root = ? AND git_branch IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (repo_root,),
        ).fetchone()
    return connection.execute(
        """
        SELECT id, repo_slug, repo_root, git_branch, git_commit, indexed_at
        FROM code_index_snapshots
        WHERE repo_root = ? AND git_branch = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (repo_root, branch),
    ).fetchone()


def _snapshot_counts(connection: sqlite3.Connection, snapshot_id: int) -> dict[str, int]:
    """Return aggregate counts for a stored code-index snapshot."""
    file_row = connection.execute(
        "SELECT COUNT(*) AS total FROM code_index_files WHERE snapshot_id = ?",
        (snapshot_id,),
    ).fetchone()
    symbol_row = connection.execute(
        """
        SELECT COUNT(*) AS total
        FROM code_symbols s
        JOIN code_index_files f ON f.id = s.file_id
        WHERE f.snapshot_id = ?
        """,
        (snapshot_id,),
    ).fetchone()
    call_row = connection.execute(
        """
        SELECT COUNT(*) AS total
        FROM code_calls c
        JOIN code_index_files f ON f.id = c.file_id
        WHERE f.snapshot_id = ?
        """,
        (snapshot_id,),
    ).fetchone()
    return {
        "files": int(file_row["total"] or 0),
        "symbols": int(symbol_row["total"] or 0),
        "calls": int(call_row["total"] or 0),
    }


def _previous_file_rows(connection: sqlite3.Connection, snapshot_id: int) -> dict[str, sqlite3.Row]:
    """Map file paths to their prior indexed rows for incremental reuse."""
    rows = connection.execute(
        """
        SELECT id, file_path, content_hash, language
        FROM code_index_files
        WHERE snapshot_id = ?
        """,
        (snapshot_id,),
    ).fetchall()
    return {str(row["file_path"]): row for row in rows}


def _copy_prior_file(
    connection: sqlite3.Connection, *, snapshot_id: int, previous_file_id: int
) -> int:
    """Copy file, symbol, and call rows from a previous snapshot into a new snapshot."""
    previous_file = connection.execute(
        """
        SELECT file_path, content_hash, language
        FROM code_index_files
        WHERE id = ?
        """,
        (previous_file_id,),
    ).fetchone()
    assert previous_file is not None
    cursor = connection.execute(
        """
        INSERT INTO code_index_files(snapshot_id, file_path, content_hash, language)
        VALUES(?, ?, ?, ?)
        """,
        (
            snapshot_id,
            previous_file["file_path"],
            previous_file["content_hash"],
            previous_file["language"],
        ),
    )
    new_file_id_raw = cursor.lastrowid
    assert new_file_id_raw is not None
    new_file_id = int(new_file_id_raw)
    connection.execute(
        """
        INSERT INTO code_symbols(
            file_id, name, qualified_name, kind, parent_qualified_name, lineno, end_lineno
        )
        SELECT ?, name, qualified_name, kind, parent_qualified_name, lineno, end_lineno
        FROM code_symbols
        WHERE file_id = ?
        """,
        (new_file_id, previous_file_id),
    )
    connection.execute(
        """
        INSERT INTO code_calls(file_id, caller_qualified_name, callee_name, lineno)
        SELECT ?, caller_qualified_name, callee_name, lineno
        FROM code_calls
        WHERE file_id = ?
        """,
        (new_file_id, previous_file_id),
    )
    return new_file_id


def index_codebase(
    connection: sqlite3.Connection,
    *,
    cwd: Path | None = None,
    repo: str | None = None,
) -> dict[str, Any]:
    """Index Python files for a local workspace into SQLite."""
    workspace = _detect_workspace(cwd, repo=repo)
    repo_root = Path(workspace["repo_root"])
    repo_slug = workspace["repo"]
    branch_name = workspace["branch"]
    git_commit = workspace["commit"]
    indexed_at = datetime.now(UTC).isoformat()

    latest_snapshot = _latest_snapshot_row(
        connection,
        repo_root=str(repo_root),
        branch=branch_name,
    )
    if latest_snapshot is not None and git_commit and latest_snapshot["git_commit"] == git_commit:
        counts = _snapshot_counts(connection, int(latest_snapshot["id"]))
        return {
            "repo": repo_slug,
            "repo_root": str(repo_root),
            "branch": branch_name,
            "commit": git_commit,
            "snapshot_id": int(latest_snapshot["id"]),
            "indexed_at": latest_snapshot["indexed_at"],
            "files_indexed": counts["files"],
            "files_parsed": 0,
            "files_reused": counts["files"],
            "symbols_indexed": counts["symbols"],
            "calls_indexed": counts["calls"],
            "skipped_files": [],
            "reused_snapshot": True,
        }

    cursor = connection.execute(
        """
        INSERT INTO code_index_snapshots(repo_slug, repo_root, git_branch, git_commit, indexed_at)
        VALUES(?, ?, ?, ?, ?)
        """,
        (repo_slug, str(repo_root), branch_name, git_commit, indexed_at),
    )
    snapshot_id_raw = cursor.lastrowid
    assert snapshot_id_raw is not None
    snapshot_id = int(snapshot_id_raw)
    previous_files = (
        _previous_file_rows(connection, int(latest_snapshot["id"]))
        if latest_snapshot is not None
        else {}
    )

    files_parsed = 0
    files_reused = 0
    symbols_indexed = 0
    calls_indexed = 0
    skipped_files: list[str] = []

    for path in sorted(_iter_python_files(repo_root)):
        try:
            relative_path = _normalize_repo_path(path, repo_root)
            content_hash = _hash_file(path)
        except (OSError, ValueError) as exc:
            LOGGER.warning("Skipping unreadable file during indexing: %s (%s)", path, exc)
            skipped_files.append(path.as_posix())
            continue

        previous_file = previous_files.get(relative_path)
        if previous_file is not None and previous_file["content_hash"] == content_hash:
            _copy_prior_file(
                connection,
                snapshot_id=snapshot_id,
                previous_file_id=int(previous_file["id"]),
            )
            files_reused += 1
            continue

        try:
            symbols, calls = _parse_python_file(path)
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            LOGGER.warning("Skipping unparsable Python file during indexing: %s (%s)", path, exc)
            skipped_files.append(relative_path)
            continue

        file_cursor = connection.execute(
            """
            INSERT INTO code_index_files(snapshot_id, file_path, content_hash, language)
            VALUES(?, ?, ?, 'python')
            """,
            (snapshot_id, relative_path, content_hash),
        )
        file_id_raw = file_cursor.lastrowid
        assert file_id_raw is not None
        file_id = int(file_id_raw)
        for symbol in symbols:
            connection.execute(
                """
                INSERT INTO code_symbols(
                    file_id, name, qualified_name, kind, parent_qualified_name, lineno, end_lineno
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    symbol["name"],
                    symbol["qualified_name"],
                    symbol["kind"],
                    symbol["parent_qualified_name"],
                    symbol["lineno"],
                    symbol["end_lineno"],
                ),
            )
        for call in calls:
            connection.execute(
                """
                INSERT INTO code_calls(file_id, caller_qualified_name, callee_name, lineno)
                VALUES(?, ?, ?, ?)
                """,
                (
                    file_id,
                    call["caller_qualified_name"],
                    call["callee_name"],
                    call["lineno"],
                ),
            )
        files_parsed += 1
        symbols_indexed += len(symbols)
        calls_indexed += len(calls)

    if files_reused:
        counts = _snapshot_counts(connection, snapshot_id)
        symbols_indexed = counts["symbols"]
        calls_indexed = counts["calls"]
    connection.commit()

    counts = _snapshot_counts(connection, snapshot_id)
    return {
        "repo": repo_slug,
        "repo_root": str(repo_root),
        "branch": branch_name,
        "commit": git_commit,
        "snapshot_id": snapshot_id,
        "indexed_at": indexed_at,
        "files_indexed": counts["files"],
        "files_parsed": files_parsed,
        "files_reused": files_reused,
        "symbols_indexed": counts["symbols"],
        "calls_indexed": counts["calls"],
        "skipped_files": skipped_files,
        "reused_snapshot": False,
    }


def _latest_snapshot_ids(
    connection: sqlite3.Connection,
    *,
    repo: str | None = None,
    branch: str | None = None,
) -> list[int]:
    """Return the latest code-index snapshot ids for the requested scope."""
    params: list[object] = []
    if repo is not None:
        normalized_repo = RepoRef.from_slug(repo).slug
        query = "SELECT id FROM code_index_snapshots WHERE repo_slug = ?"
        params.append(normalized_repo)
        if branch is None:
            query += " ORDER BY id DESC LIMIT 1"
        else:
            query += " AND git_branch = ? ORDER BY id DESC LIMIT 1"
            params.append(branch)
        row = connection.execute(query, params).fetchone()
        return [int(row["id"])] if row is not None else []

    query = "SELECT MAX(id) AS id FROM code_index_snapshots"
    if branch is not None:
        query += " WHERE git_branch = ?"
        params.append(branch)
    query += " GROUP BY COALESCE(repo_slug, repo_root)"
    return [
        int(row["id"] or 0) for row in connection.execute(query, params).fetchall() if row["id"]
    ]


def _snapshot_in_clause(snapshot_ids: list[int]) -> tuple[str, list[object]]:
    """Build a SQLite IN clause for a snapshot-id list."""
    placeholders = ", ".join("?" for _ in snapshot_ids)
    return f"({placeholders})", [*snapshot_ids]


def _match_symbol_candidates(symbol: str) -> list[str]:
    """Return exact and short-name variants for symbol matching."""
    normalized = symbol.strip()
    if not normalized:
        return []
    candidates = [normalized]
    short_name = normalized.rsplit(".", maxsplit=1)[-1]
    if short_name not in candidates:
        candidates.append(short_name)
    return candidates


def search_symbols(
    connection: sqlite3.Connection,
    *,
    query: str,
    repo: str | None = None,
    branch: str | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Search indexed symbols from the latest snapshot scope."""
    snapshot_ids = _latest_snapshot_ids(connection, repo=repo, branch=branch)
    if not snapshot_ids:
        return []
    in_clause, snapshot_params = _snapshot_in_clause(snapshot_ids)
    query_like = f"%{query.lower()}%"
    rows = connection.execute(
        f"""
        SELECT
            snap.repo_slug,
            snap.repo_root,
            snap.git_branch,
            snap.git_commit,
            f.file_path,
            s.name,
            s.qualified_name,
            s.kind,
            s.lineno
        FROM code_symbols s
        JOIN code_index_files f ON f.id = s.file_id
        JOIN code_index_snapshots snap ON snap.id = f.snapshot_id
        WHERE f.snapshot_id IN {in_clause}
          AND (
            LOWER(s.name) LIKE ?
            OR LOWER(s.qualified_name) LIKE ?
          )
        ORDER BY
            CASE
                WHEN LOWER(s.qualified_name) = ? THEN 0
                WHEN LOWER(s.name) = ? THEN 1
                ELSE 2
            END,
            s.kind,
            s.qualified_name,
            f.file_path
        LIMIT ?
        """,
        [*snapshot_params, query_like, query_like, query.lower(), query.lower(), max(1, limit)],
    ).fetchall()
    return [
        {
            "repo": row["repo_slug"] or row["repo_root"],
            "branch": row["git_branch"],
            "commit": row["git_commit"],
            "file_path": row["file_path"],
            "name": row["name"],
            "qualified_name": row["qualified_name"],
            "kind": row["kind"],
            "line_number": row["lineno"],
            "provenance": build_provenance(
                source_type="code_symbol",
                source_id=f"{row['qualified_name']}:{row['file_path']}:{row['lineno']}",
                confidence=(
                    "HIGH"
                    if query.lower()
                    in {str(row["name"]).lower(), str(row["qualified_name"]).lower()}
                    else "MEDIUM"
                ),
                retrieval_reason="symbol_name_match",
                reason_detail=f"Indexed symbol match in {row['file_path']}",
            ),
        }
        for row in rows
    ]


def get_symbol_callers(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    repo: str | None = None,
    branch: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return indexed call sites that invoke the requested symbol."""
    snapshot_ids = _latest_snapshot_ids(connection, repo=repo, branch=branch)
    candidates = _match_symbol_candidates(symbol)
    if not snapshot_ids or not candidates:
        return []
    in_clause, snapshot_params = _snapshot_in_clause(snapshot_ids)
    candidate_placeholders = ", ".join("?" for _ in candidates)
    rows = connection.execute(
        f"""
        SELECT
            snap.repo_slug,
            snap.repo_root,
            snap.git_branch,
            f.file_path,
            c.caller_qualified_name,
            c.callee_name,
            c.lineno
        FROM code_calls c
        JOIN code_index_files f ON f.id = c.file_id
        JOIN code_index_snapshots snap ON snap.id = f.snapshot_id
        WHERE f.snapshot_id IN {in_clause}
          AND c.callee_name IN ({candidate_placeholders})
        ORDER BY f.file_path, c.caller_qualified_name, c.lineno
        LIMIT ?
        """,
        [*snapshot_params, *candidates, max(1, limit)],
    ).fetchall()
    return [
        {
            "repo": row["repo_slug"] or row["repo_root"],
            "branch": row["git_branch"],
            "file_path": row["file_path"],
            "caller": row["caller_qualified_name"],
            "callee": row["callee_name"],
            "line_number": row["lineno"],
            "provenance": build_provenance(
                source_type="code_call",
                source_id=f"{row['caller_qualified_name']}->{row['callee_name']}:{row['file_path']}",
                confidence="MEDIUM",
                retrieval_reason="symbol_caller_relationship",
                reason_detail=f"Indexed caller relationship in {row['file_path']}",
            ),
        }
        for row in rows
    ]


def get_symbol_callees(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    repo: str | None = None,
    branch: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return indexed outgoing calls made by the requested symbol."""
    snapshot_ids = _latest_snapshot_ids(connection, repo=repo, branch=branch)
    candidates = _match_symbol_candidates(symbol)
    if not snapshot_ids or not candidates:
        return []
    in_clause, snapshot_params = _snapshot_in_clause(snapshot_ids)
    rows = connection.execute(
        f"""
        SELECT
            snap.repo_slug,
            snap.repo_root,
            snap.git_branch,
            f.file_path,
            c.caller_qualified_name,
            c.callee_name,
            c.lineno
        FROM code_calls c
        JOIN code_index_files f ON f.id = c.file_id
        JOIN code_index_snapshots snap ON snap.id = f.snapshot_id
        WHERE f.snapshot_id IN {in_clause}
          AND (
            c.caller_qualified_name = ?
            OR c.caller_qualified_name LIKE ?
          )
        ORDER BY c.lineno, c.callee_name, f.file_path
        LIMIT ?
        """,
        [
            *snapshot_params,
            candidates[0],
            f"%.{candidates[-1]}",
            max(1, limit),
        ],
    ).fetchall()
    return [
        {
            "repo": row["repo_slug"] or row["repo_root"],
            "branch": row["git_branch"],
            "file_path": row["file_path"],
            "caller": row["caller_qualified_name"],
            "callee": row["callee_name"],
            "line_number": row["lineno"],
            "provenance": build_provenance(
                source_type="code_call",
                source_id=f"{row['caller_qualified_name']}->{row['callee_name']}:{row['file_path']}",
                confidence="MEDIUM",
                retrieval_reason="symbol_callee_relationship",
                reason_detail=f"Indexed callee relationship in {row['file_path']}",
            ),
        }
        for row in rows
    ]


def get_impacted_files(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    repo: str | None = None,
    branch: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return files most directly impacted by a symbol and its callers."""
    definitions = search_symbols(connection, query=symbol, repo=repo, branch=branch, limit=limit)
    callers = get_symbol_callers(connection, symbol=symbol, repo=repo, branch=branch, limit=limit)
    impacted: dict[tuple[str, str, str | None], dict[str, Any]] = {}

    for row in definitions:
        key = (row["repo"], row["file_path"], row["branch"])
        impacted.setdefault(
            key,
            {
                "repo": row["repo"],
                "branch": row["branch"],
                "file_path": row["file_path"],
                "reasons": set(),
            },
        )["reasons"].add("defines symbol")
    for row in callers:
        key = (row["repo"], row["file_path"], row["branch"])
        impacted.setdefault(
            key,
            {
                "repo": row["repo"],
                "branch": row["branch"],
                "file_path": row["file_path"],
                "reasons": set(),
            },
        )["reasons"].add("calls symbol")

    return [
        {
            "repo": value["repo"],
            "branch": value["branch"],
            "file_path": value["file_path"],
            "reasons": sorted(value["reasons"]),
            "provenance": build_provenance(
                source_type="code_file",
                source_id=f"{value['repo']}:{value['file_path']}",
                confidence="MEDIUM",
                retrieval_reason="symbol_relationship",
                reason_detail=f"Impacted by indexed symbol relationships in {value['file_path']}",
            ),
        }
        for value in list(impacted.values())[: max(1, limit)]
    ]


def _normalize_lookup_file_path(file_path: str, repo_root: Path | None = None) -> str:
    """Normalize a user-provided file path into a repo-relative path when possible."""
    raw = Path(file_path)
    if repo_root is not None:
        resolved_root = repo_root.resolve()
        if raw.is_absolute():
            try:
                return raw.resolve().relative_to(resolved_root).as_posix()
            except ValueError:
                return raw.as_posix()
        try:
            return (resolved_root / raw).resolve().relative_to(resolved_root).as_posix()
        except ValueError:
            return raw.as_posix()
    return raw.as_posix().lstrip("./")


def _file_path_matches(indexed_path: str, requested_path: str) -> bool:
    """Match repo-relative file paths with exact or suffix semantics."""
    normalized_indexed = indexed_path.replace("\\", "/")
    normalized_requested = requested_path.replace("\\", "/")
    return (
        normalized_indexed == normalized_requested
        or normalized_indexed.endswith(f"/{normalized_requested}")
        or normalized_requested.endswith(f"/{normalized_indexed}")
    )


def _latest_snapshot_for_context(
    connection: sqlite3.Connection,
    *,
    repo: str | None = None,
    branch: str | None = None,
    cwd: Path | None = None,
) -> sqlite3.Row | None:
    """Pick the latest snapshot for file-context lookups."""
    params: list[object] = []
    query = (
        "SELECT id, repo_slug, repo_root, git_branch, git_commit, indexed_at "
        "FROM code_index_snapshots WHERE 1 = 1"
    )
    if repo is not None:
        query += " AND repo_slug = ?"
        params.append(RepoRef.from_slug(repo).slug)
    elif cwd is not None:
        repo_root = str(_detect_workspace(cwd)["repo_root"])
        query += " AND repo_root = ?"
        params.append(repo_root)
    if branch is not None:
        query += " AND git_branch = ?"
        params.append(branch)
    query += " ORDER BY id DESC LIMIT 1"
    return connection.execute(query, params).fetchone()


def get_file_review_history(
    connection: sqlite3.Connection,
    *,
    repo: str,
    file_path: str,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Return historical review discussions that touched a file."""
    repo_ref = RepoRef.from_slug(repo)
    requested_path = file_path.replace("\\", "/")
    rows = connection.execute(
        """
        SELECT
            p.number AS pr_number,
            p.title AS pr_title,
            p.state AS pr_state,
            p.updated_at,
            t.id AS thread_id,
            t.file_path,
            t.thread_state,
            (
                SELECT rc.author
                FROM review_comments rc
                WHERE rc.thread_id = t.id
                ORDER BY rc.created_at ASC, rc.id ASC
                LIMIT 1
            ) AS reviewer,
            (
                SELECT rc.body
                FROM review_comments rc
                WHERE rc.thread_id = t.id
                ORDER BY rc.created_at DESC, rc.id DESC
                LIMIT 1
            ) AS last_body,
            latest_cache.decision_type,
            latest_cache.summary
        FROM review_threads t
        JOIN prs p ON p.id = t.pr_id
        JOIN repos r ON r.id = p.repo_id
        LEFT JOIN llm_cache latest_cache ON latest_cache.comment_id = (
            SELECT rc.id
            FROM review_comments rc
            JOIN llm_cache cache ON cache.comment_id = rc.id
            WHERE rc.thread_id = t.id
            ORDER BY rc.created_at DESC, rc.id DESC
            LIMIT 1
        )
        WHERE r.owner = ? AND r.name = ? AND t.file_path IS NOT NULL
        ORDER BY p.updated_at DESC, t.updated_at DESC, t.id DESC
        LIMIT ?
        """,
        (repo_ref.owner, repo_ref.name, max(1, limit * 4)),
    ).fetchall()
    history = [
        {
            "pr_number": row["pr_number"],
            "pr_title": row["pr_title"],
            "pr_state": row["pr_state"],
            "updated_at": row["updated_at"],
            "thread_id": row["thread_id"],
            "file_path": row["file_path"],
            "thread_state": row["thread_state"],
            "reviewer": row["reviewer"] or "unknown",
            "decision_type": row["decision_type"] or "QUESTION",
            "summary": row["summary"] or row["last_body"] or "",
            "provenance": build_provenance(
                source_type="review_thread",
                source_id=str(row["thread_id"]),
                confidence="HIGH",
                retrieval_reason="exact_file_match",
                reason_detail=f"Historical review thread on {row['file_path']}",
            ),
        }
        for row in rows
        if _file_path_matches(str(row["file_path"]), requested_path)
    ]
    return history[: max(1, limit)]


def get_combined_file_context(
    connection: sqlite3.Connection,
    *,
    file_path: str,
    repo: str | None = None,
    branch: str | None = None,
    cwd: Path | None = None,
    explain: bool = False,
) -> dict[str, Any]:
    """Combine indexed code context with historical review context for a file."""
    snapshot = _latest_snapshot_for_context(connection, repo=repo, branch=branch, cwd=cwd)
    if snapshot is None:
        raise ValueError("No code index found for this scope. Run `oss-context code index` first.")

    repo_root = Path(snapshot["repo_root"])
    normalized_path = _normalize_lookup_file_path(file_path, repo_root)
    file_rows = connection.execute(
        """
        SELECT id, file_path
        FROM code_index_files
        WHERE snapshot_id = ?
        ORDER BY file_path ASC
        """,
        (snapshot["id"],),
    ).fetchall()
    matching_file = next(
        (row for row in file_rows if _file_path_matches(str(row["file_path"]), normalized_path)),
        None,
    )
    if matching_file is None:
        raise ValueError(f"File {file_path!r} was not found in the indexed snapshot.")

    symbols = [
        {
            "name": row["name"],
            "qualified_name": row["qualified_name"],
            "kind": row["kind"],
            "line_number": row["lineno"],
            "end_line_number": row["end_lineno"],
            "provenance": build_provenance(
                source_type="code_symbol",
                source_id=f"{row['qualified_name']}:{matching_file['file_path']}:{row['lineno']}",
                confidence="HIGH",
                retrieval_reason="exact_file_match",
                reason_detail=f"Indexed symbol defined in {matching_file['file_path']}",
            ),
        }
        for row in connection.execute(
            """
            SELECT name, qualified_name, kind, lineno, end_lineno
            FROM code_symbols
            WHERE file_id = ?
            ORDER BY lineno ASC, qualified_name ASC
            """,
            (matching_file["id"],),
        ).fetchall()
    ]
    outbound_rows = connection.execute(
        """
        SELECT callee_name, COUNT(*) AS call_count, MIN(lineno) AS first_line
        FROM code_calls
        WHERE file_id = ?
        GROUP BY callee_name
        ORDER BY call_count DESC, callee_name ASC
        LIMIT 25
        """,
        (matching_file["id"],),
    ).fetchall()
    symbol_names = {row["name"] for row in symbols}
    inbound_calls: list[dict[str, Any]] = []
    if symbol_names:
        placeholders = ", ".join("?" for _ in symbol_names)
        inbound_calls = [
            {
                "repo": row["repo_slug"] or row["repo_root"],
                "branch": row["git_branch"],
                "file_path": row["file_path"],
                "caller": row["caller_qualified_name"],
                "callee": row["callee_name"],
                "line_number": row["lineno"],
                "provenance": build_provenance(
                    source_type="code_call",
                    source_id=f"{row['caller_qualified_name']}->{row['callee_name']}:{row['file_path']}",
                    confidence="MEDIUM",
                    retrieval_reason="symbol_caller_relationship",
                    reason_detail=f"Indexed caller relationship into {matching_file['file_path']}",
                ),
            }
            for row in connection.execute(
                f"""
                SELECT
                    snap.repo_slug,
                    snap.repo_root,
                    snap.git_branch,
                    f.file_path,
                    c.caller_qualified_name,
                    c.callee_name,
                    c.lineno
                FROM code_calls c
                JOIN code_index_files f ON f.id = c.file_id
                JOIN code_index_snapshots snap ON snap.id = f.snapshot_id
                WHERE f.snapshot_id = ?
                  AND c.callee_name IN ({placeholders})
                  AND f.id != ?
                ORDER BY f.file_path, c.lineno
                LIMIT 25
                """,
                (snapshot["id"], *sorted(symbol_names), matching_file["id"]),
            ).fetchall()
        ]

    repo_slug = snapshot["repo_slug"]
    review_history = (
        get_file_review_history(
            connection,
            repo=repo_slug,
            file_path=str(matching_file["file_path"]),
            limit=15,
        )
        if repo_slug
        else []
    )
    outbound_calls = [
        {
            "callee": row["callee_name"],
            "call_count": int(row["call_count"] or 0),
            "first_line": row["first_line"],
            "provenance": build_provenance(
                source_type="code_call",
                source_id=f"{matching_file['file_path']}->{row['callee_name']}:{row['first_line']}",
                confidence="MEDIUM",
                retrieval_reason="symbol_callee_relationship",
                reason_detail=f"Indexed outgoing call from {matching_file['file_path']}",
            ),
        }
        for row in outbound_rows
    ]
    provenance_items = [
        *symbols,
        *outbound_calls,
        *inbound_calls,
        *review_history,
    ]
    return {
        "repo": repo_slug,
        "repo_root": snapshot["repo_root"],
        "branch": snapshot["git_branch"],
        "commit": snapshot["git_commit"],
        "indexed_at": snapshot["indexed_at"],
        "file_path": matching_file["file_path"],
        "symbols": symbols,
        "outbound_calls": outbound_calls,
        "inbound_calls": inbound_calls,
        "review_history": review_history,
        "explain": explain,
        "retrieval_explanations": summarize_provenance(provenance_items),
    }
