"""SQLite schema and connection helpers for oss-context.

This module owns database initialization, schema creation, and connection
settings such as WAL mode so sync, issue lookup, reference extraction, and
query operations share the same storage behavior.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY,
    github_id INTEGER UNIQUE,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    default_branch TEXT,
    last_synced_at TIMESTAMP,
    UNIQUE(owner, name)
);

CREATE TABLE IF NOT EXISTS prs (
    id INTEGER PRIMARY KEY,
    github_id INTEGER UNIQUE,
    repo_id INTEGER NOT NULL REFERENCES repos(id),
    number INTEGER NOT NULL,
    title TEXT,
    state TEXT,
    author TEXT,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    body TEXT,
    base_branch TEXT,
    head_branch TEXT,
    merge_commit_sha TEXT,
    UNIQUE(repo_id, number)
);

CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY,
    github_id INTEGER UNIQUE,
    repo_id INTEGER NOT NULL REFERENCES repos(id),
    number INTEGER NOT NULL,
    title TEXT,
    state TEXT,
    author TEXT,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    closed_at TIMESTAMP,
    body TEXT,
    UNIQUE(repo_id, number)
);

CREATE TABLE IF NOT EXISTS review_threads (
    id INTEGER PRIMARY KEY,
    github_thread_id TEXT UNIQUE,
    pr_id INTEGER NOT NULL REFERENCES prs(id),
    file_path TEXT,
    line_number INTEGER,
    thread_state TEXT,
    resolved_by TEXT,
    resolved_at TIMESTAMP,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS review_comments (
    id INTEGER PRIMARY KEY,
    thread_id INTEGER NOT NULL REFERENCES review_threads(id),
    github_comment_id INTEGER UNIQUE,
    author TEXT,
    body TEXT,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    reaction_count INTEGER DEFAULT 0,
    is_suggestion BOOLEAN DEFAULT 0,
    suggestion_applied BOOLEAN DEFAULT 0,
    extracted_decision TEXT,
    decision_confidence REAL
);

CREATE TABLE IF NOT EXISTS decision_log (
    id INTEGER PRIMARY KEY,
    pr_id INTEGER NOT NULL REFERENCES prs(id),
    comment_id INTEGER NOT NULL REFERENCES review_comments(id),
    decision_type TEXT,
    extracted_summary TEXT,
    raw_text TEXT,
    raw_text_hash TEXT,
    extracted_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pr_labels (
    pr_id INTEGER NOT NULL REFERENCES prs(id),
    label TEXT NOT NULL,
    added_at TIMESTAMP,
    PRIMARY KEY(pr_id, label)
);

CREATE TABLE IF NOT EXISTS issue_labels (
    issue_id INTEGER NOT NULL REFERENCES issues(id),
    label TEXT NOT NULL,
    added_at TIMESTAMP,
    PRIMARY KEY(issue_id, label)
);

CREATE TABLE IF NOT EXISTS issue_comments (
    id INTEGER PRIMARY KEY,
    issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    github_comment_id INTEGER UNIQUE,
    author TEXT,
    body TEXT,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    reaction_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS extracted_references (
    id INTEGER PRIMARY KEY,
    source_kind TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    repo_id INTEGER REFERENCES repos(id),
    reference_kind TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    url TEXT,
    target_repo TEXT,
    target_number INTEGER,
    target_sha TEXT,
    title TEXT
);

CREATE TABLE IF NOT EXISTS architectural_decisions (
    id INTEGER PRIMARY KEY,
    repo_id INTEGER NOT NULL REFERENCES repos(id),
    pr_id INTEGER REFERENCES prs(id),
    issue_id INTEGER REFERENCES issues(id),
    summary TEXT NOT NULL,
    rationale TEXT,
    alternatives TEXT,
    outcome TEXT,
    created_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS design_summaries (
    id INTEGER PRIMARY KEY,
    repo_id INTEGER NOT NULL REFERENCES repos(id),
    target_type TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    summary TEXT NOT NULL,
    generated_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS implementation_summaries (
    id INTEGER PRIMARY KEY,
    repo_id INTEGER NOT NULL REFERENCES repos(id),
    target_type TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    summary TEXT NOT NULL,
    generated_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS rationale_links (
    id INTEGER PRIMARY KEY,
    repo_id INTEGER NOT NULL REFERENCES repos(id),
    source_type TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relationship TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_cache (
    comment_id INTEGER PRIMARY KEY REFERENCES review_comments(id),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    confidence REAL NOT NULL,
    analyzed_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS branch_links (
    id INTEGER PRIMARY KEY,
    repo_slug TEXT NOT NULL,
    branch_name TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    linked_at TIMESTAMP NOT NULL,
    UNIQUE(repo_slug, branch_name)
);

CREATE TABLE IF NOT EXISTS code_index_snapshots (
    id INTEGER PRIMARY KEY,
    repo_slug TEXT,
    repo_root TEXT NOT NULL,
    git_branch TEXT,
    git_commit TEXT,
    indexed_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS code_index_files (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES code_index_snapshots(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'python',
    UNIQUE(snapshot_id, file_path)
);

CREATE TABLE IF NOT EXISTS code_symbols (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES code_index_files(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    parent_qualified_name TEXT,
    lineno INTEGER,
    end_lineno INTEGER
);

CREATE TABLE IF NOT EXISTS code_calls (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES code_index_files(id) ON DELETE CASCADE,
    caller_qualified_name TEXT NOT NULL,
    callee_name TEXT NOT NULL,
    lineno INTEGER
);

CREATE INDEX IF NOT EXISTS idx_prs_repo_state ON prs(repo_id, state);
CREATE INDEX IF NOT EXISTS idx_issues_repo_state ON issues(repo_id, state);
CREATE INDEX IF NOT EXISTS idx_threads_pr_state ON review_threads(pr_id, thread_state);
CREATE INDEX IF NOT EXISTS idx_comments_thread ON review_comments(thread_id);
CREATE INDEX IF NOT EXISTS idx_decisions_pr ON decision_log(pr_id);
CREATE INDEX IF NOT EXISTS idx_pr_labels_label ON pr_labels(label);
CREATE INDEX IF NOT EXISTS idx_issue_labels_label ON issue_labels(label);
CREATE INDEX IF NOT EXISTS idx_issue_comments_issue ON issue_comments(issue_id);
CREATE INDEX IF NOT EXISTS idx_branch_links_branch ON branch_links(branch_name);
CREATE INDEX IF NOT EXISTS idx_code_snapshots_repo_branch ON code_index_snapshots(
    repo_slug, repo_root, git_branch, indexed_at
);
CREATE INDEX IF NOT EXISTS idx_code_snapshots_root_branch ON code_index_snapshots(
    repo_root, git_branch
);
CREATE INDEX IF NOT EXISTS idx_code_symbols_name ON code_symbols(name, qualified_name, kind);
CREATE INDEX IF NOT EXISTS idx_code_symbols_file ON code_symbols(file_id);
CREATE INDEX IF NOT EXISTS idx_code_calls_callee ON code_calls(callee_name, caller_qualified_name);
CREATE INDEX IF NOT EXISTS idx_code_calls_file ON code_calls(file_id);
CREATE INDEX IF NOT EXISTS idx_refs_source ON extracted_references(source_kind, source_id);
CREATE INDEX IF NOT EXISTS idx_refs_target ON extracted_references(
    target_repo, target_number, reference_kind
);
CREATE INDEX IF NOT EXISTS idx_adr_repo ON architectural_decisions(repo_id);
CREATE INDEX IF NOT EXISTS idx_design_target ON design_summaries(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_impl_target ON implementation_summaries(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_rationale_source ON rationale_links(source_type, source_id);
"""


class DatabaseManager:
    def __init__(self, path: Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON;")
        connection.execute("PRAGMA journal_mode = WAL;")
        connection.execute("PRAGMA synchronous = NORMAL;")
        return connection

    def initialize(self) -> sqlite3.Connection:
        connection = self.connect()
        connection.executescript(SCHEMA)
        
        # Migrations
        try:
            connection.execute("ALTER TABLE decision_log ADD COLUMN decision_status TEXT")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise
        try:
            connection.execute("ALTER TABLE decision_log ADD COLUMN decision_reason TEXT")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise

        try:
            connection.execute("ALTER TABLE extracted_references ADD COLUMN title TEXT")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise

        # Migration to create issue_comments table and index if they don't exist dynamically
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS issue_comments (
            id INTEGER PRIMARY KEY,
            issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
            github_comment_id INTEGER UNIQUE,
            author TEXT,
            body TEXT,
            created_at TIMESTAMP,
            updated_at TIMESTAMP,
            reaction_count INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_issue_comments_issue ON issue_comments(issue_id);
        """)
                
        # Phase 6 migrations for adding repo_id to caches with strict constraints
        for table in ["design_summaries", "implementation_summaries", "rationale_links"]:
            cursor = connection.execute(f"PRAGMA table_info({table})")
            columns = {row["name"]: row for row in cursor.fetchall()}
            
            needs_rebuild = False
            if "repo_id" not in columns:
                needs_rebuild = True
            elif not columns["repo_id"]["notnull"]:
                needs_rebuild = True
                
            if needs_rebuild:
                # Disable foreign keys for the rebuild
                # Must be outside transaction
                connection.commit()
                connection.execute("PRAGMA foreign_keys = OFF;")
                try:
                    # Rename old table
                    connection.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
                    
                    # Recreate with new strict schema
                    connection.executescript(SCHEMA)
                    
                    # Backfill data from old table, attempting to guess repo_id if missing.
                    # If we can't safely resolve repo_id (e.g. multiple repos have PR #42), we discard the cache row.  # noqa: E501
                    
                    # Try to map target_id (PR/Issue number) to a single repo_id
                    if table in ("design_summaries", "implementation_summaries"):
                        connection.execute(f"""
                            INSERT INTO {table} (
                                id, repo_id, target_type, target_id, summary, generated_at
                                {', file_path' if table == 'implementation_summaries' else ''}
                            )
                            SELECT o.id, 
                                   COALESCE(
                                       (SELECT repo_id FROM prs 
                                        WHERE number = o.target_id 
                                          AND o.target_type = 'pr' LIMIT 1),
                                       (SELECT repo_id FROM issues 
                                        WHERE number = o.target_id 
                                          AND o.target_type = 'issue' LIMIT 1)
                                   ),
                                   o.target_type, o.target_id, o.summary, o.generated_at
                                   {', o.file_path' if table == 'implementation_summaries' else ''}
                            FROM {table}_old o
                            WHERE (SELECT count(repo_id) FROM prs 
                                   WHERE number = o.target_id AND o.target_type = 'pr') = 1
                               OR (SELECT count(repo_id) FROM issues 
                                   WHERE number = o.target_id AND o.target_type = 'issue') = 1
                        """)
                    elif table == "rationale_links":
                        connection.execute(f"""
                            INSERT INTO {table} (
                                id, repo_id, source_type, source_id, 
                                target_type, target_id, relationship
                            )
                            SELECT o.id, 
                                   COALESCE(
                                       (SELECT repo_id FROM prs 
                                        WHERE number = o.source_id 
                                          AND o.source_type = 'design_summary' LIMIT 1),
                                       (SELECT repo_id FROM issues 
                                        WHERE number = o.source_id 
                                          AND o.source_type = 'design_summary' LIMIT 1)
                                   ),
                                   o.source_type, o.source_id, o.target_type, o.target_id, 
                                   o.relationship
                            FROM {table}_old o
                            WHERE (SELECT count(repo_id) FROM prs 
                                   WHERE number = o.source_id 
                                     AND o.source_type = 'design_summary') = 1
                               OR (SELECT count(repo_id) FROM issues 
                                   WHERE number = o.source_id 
                                     AND o.source_type = 'design_summary') = 1
                        """)
                        
                    connection.execute(f"DROP TABLE {table}_old")
                finally:
                    connection.commit()
                    connection.execute("PRAGMA foreign_keys = ON;")
                    
        # Re-run schema to recreate indexes that were attached to {table}_old and dropped
        connection.executescript(SCHEMA)
            
        connection.commit()
        return connection
