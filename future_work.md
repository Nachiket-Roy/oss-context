<!-- Deferred roadmap notes for oss-context. -->
# future_work.md

## Future Work

These items are intentionally deferred. The current local-first workflows now cover synced PR and issue state, branch-aware context, lightweight code indexing, merge-readiness summaries, and a local HTML UI; the next set of improvements focuses on deepening repository intelligence, proactive assistance, richer editor integrations, and operational resilience.

### Repository intelligence and code indexing

- Expand indexing beyond Python AST parsing into richer symbol resolution and import-aware call graphs
- Drive indexing incrementally from git diffs and filesystem change events instead of full workspace scans
- Compare snapshots across branches and commits to highlight impacted symbols and files during context switches
- Improve retrieval APIs that blend code context with PR reviews, issue discussions, and linked references
- Enrich file history views with broader maintainer discussion context and older review decisions

### Review assistant workflows

- Draft candidate responses for review threads and unresolved questions
- Refine merge-readiness scoring and follow-up recommendations with better reviewer-state heuristics
- Build stronger cross-PR and issue-aware summaries from the extracted reference graph
- Add assistant flows that answer "what is left before this PR can merge?" at thread and reviewer granularity

### UX extensions

- Add saved filters, review queues, and faster search-first navigation in the local UI
- Expose branch-aware editor/file resources more directly for MCP clients and active-editor integrations
- Surface freshness indicators and optional background sync status inside editor workflows
- Automatically inject relevant review and issue context for the file currently being edited

### Operational hardening

- Add stronger telemetry and logging around sync, indexing, and branch-resolution failures
- Expand integration coverage for `gh` fallback, hook installation, code indexing, and multi-repo workflows
- Support export/import paths for backups, migration, and local database recovery

### Retrieval quality and provenance

- Expand confidence scoring across more retrieval surfaces and resource types
- Add deeper explainable retrieval output and richer exclusion reporting
- Keep provenance metadata attached to all returned context items and UI views
- Grow regression suites for retrieval quality, false positives, branch switching, and file moves
- Add configurable thresholds once opt-in semantic retrieval exists

## Long-term ideas

These ideas remain intentionally deferred until the core local-first workflows mature further.

- Slack or Discord webhooks for stale unresolved threads
- Local terminal notifications after sync when new blocking reviews land
- Optional GitHub Actions summaries posted back to PRs
- Shared or replicated databases for team-wide deployments
