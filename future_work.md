<!-- Future roadmap notes for oss-context. -->
# future_work.md

## Phase 5: Advanced Features

Phase 5 is intentionally future work. The Phase 4 branch-context integration now
bridges local development workflows to synced PR review state; the next phase
focuses on proactive assistance, notifications, and broader collaboration.

### 5.1 Notification integration
- Slack or Discord webhooks for stale unresolved threads
- Local terminal notifications after sync when new blocking reviews land
- Optional GitHub Actions summaries posted back to PRs

### 5.2 Team-synced database
- Read-only replicas for teams using a shared object store or network volume
- Explicit single-writer or replication strategy for SQLite durability
- Operational docs for backup, retention, and conflict recovery

### 5.3 Review assistant workflows
- "What is left before this PR can merge?" summaries
- Response drafting assistance for review comments
- Merge-readiness scoring and reviewer follow-up recommendations
- Cross-PR and issue-aware summaries using the extracted reference graph

### 5.4 UX extensions
- Search-first UI with saved filters and review queues
- Branch-aware MCP resources for open editors and active files
- Optional background sync with freshness indicators in editors

### 5.5 Operational hardening
- Better telemetry/logging around sync and branch resolution failures
- More integration tests for `gh` fallback, hook installation, and multi-repo workflows
- Export/import paths for backups and data migration
