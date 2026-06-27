# oss-context

`oss-context` tracks pull request review decisions and reviewer state across GitHub PRs using a local SQLite knowledge graph.

This repository now contains the core foundation for local sync, review intelligence, IDE integration, and dashboard workflows:

- GitHub sync into SQLite with incremental PR discovery
- Review thread + comment persistence
- Decision extraction with LLM providers or heuristic fallback
- Query-oriented CLI with rich terminal output
- MCP server integration for IDE and agent workflows
- Cross-repo dashboard and reviewer-load queries
- Basic CI for linting and tests

## Status

Implemented in this branch:

- project scaffolding, schema, GitHub sync engine, and CLI basics
- decision extraction, context assembly, health summaries, and rich output
- MCP tools and resources for PR context, issue context, and unresolved state
- cross-repo queries, reviewer-status summaries, dashboard metrics, and a local HTML UI
- branch-aware PR resolution, file-level context, and warning-only git hook installation

Future work is tracked in `future_work.md`

## Installation

This README is the primary user and contributor guide for the repository.
It explains setup, configuration, and the local workflow for running the same
checks enforced by CI.


Requires Python 3.12 or newer.

### With `uv`

```bash
uv sync --extra dev
uv run oss-context --help
uv run pyright
```

### With `pip`

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
oss-context --help
pyright
```

## Environment variables

### GitHub

- `GITHUB_TOKEN` - recommended for GitHub API access
- `OSS_CONTEXT_DB_PATH` - override the SQLite database path

### LLM decision extraction

- `OSS_CONTEXT_LLM_PROVIDER` - `heuristic`, `openai`, or `anthropic`
- `OSS_CONTEXT_LLM_MODEL` - optional model override
- `OSS_CONTEXT_LLM_API_KEY` - provider key override
- `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` - provider-native fallbacks

If no LLM provider is configured, `oss-context` falls back to a deterministic heuristic classifier so review classification remains usable without secrets.

## CLI examples

```bash
# Sync a repository into the local knowledge graph
oss-context sync owner/repo

# Show unresolved threads across all synced repositories
oss-context query --unresolved

# Show unresolved threads for a repo
oss-context query --repo owner/repo --unresolved

# Show extracted decisions for a specific PR
oss-context query --repo owner/repo --pr 42 --decisions

# Show full PR context, including linked references
oss-context query --repo owner/repo --pr 42 --context

# Show issue context even when the issue was never mentioned in review comments
oss-context query --repo owner/repo --issue 44

# Show PR health
oss-context query --repo owner/repo --pr 42 --health

# Show reviewer status explicitly
oss-context query --reviewer bob --reviewer-status

# Show a cross-repo dashboard
oss-context query --dashboard

# Show reviewer-specific pending work across repos
oss-context query --reviewer bob --pending

# Show tracked repository sync status
oss-context query --repos

# Resolve the current branch to its PR
oss-context branch current-pr

# Show branch-aware PR context for the current worktree
oss-context branch context

# Show file-level unresolved review context for the current branch PR
oss-context branch file-context src/auth.py

# Manually pin the current branch to a synced PR
oss-context branch link --repo owner/repo --pr 42

# Install warning-only git hooks for branch-aware review reminders
oss-context install-hooks
```

## MCP server usage

Run the local MCP server over stdio for IDE integrations:

```bash
uv run oss-context serve
```

Run it over HTTP instead:

```bash
uv run oss-context serve --transport http --host 127.0.0.1 --port 8765
```

Run the local HTML UI instead of the MCP server:

```bash
uv run oss-context ui --host 127.0.0.1 --port 8080
```

Useful UI routes:

- `/` — dashboard
- `/repo/{owner}/{name}` — repo overview
- `/repo/{owner}/{name}/issues` — repo issue list
- `/pr/{owner}/{name}/{number}` — PR detail
- `/issue/{owner}/{name}/{number}` — issue detail

Exposed MCP tools include:

- `sync_repo`
- `get_pr_context`
- `get_issue_context`
- `get_unresolved_threads`
- `get_reviewer_state`
- `get_dashboard`
- `search_work`

Exposed MCP resources include:

- `pr://{owner}/{name}/{pr_number}/context`
- `issue://{owner}/{name}/{issue_number}/context`
- `pr://{owner}/{name}/unresolved`
- `pr://{owner}/{name}/freshness`
- `pr://dashboard/overview`
- `pr://reviewer/{reviewer}/status`

## Running the CI checks locally

Use the same commands as the GitHub Actions workflow:

```bash
uv sync --extra dev
uv run ruff check .
uv run pyright
uv run pytest
```

Or run them as a single command:

```bash
uv sync --extra dev && uv run ruff check . && uv run pyright && uv run pytest
```

## Notes

- The SQLite database runs in WAL mode so sync and query operations can safely overlap.
- Review thread state comes from GitHub review threads via GraphQL.
- Repository sync now includes GitHub issues, so issues can be queried directly by number.
- Structured references are extracted from PR bodies, issue bodies, and review comments.
- Decision extraction is cached per comment body hash to avoid repeat analysis cost.
- `oss-context serve` starts the FastMCP server for IDE and agent integrations.
- `oss-context ui` starts a local-only HTML dashboard backed by the same SQLite database.
- `oss-context branch ...` bridges the current git worktree to synced PR review state.
- `oss-context install-hooks` installs warning-only git hooks and refuses to overwrite unmanaged hooks.
- Cross-repo dashboard queries are available directly from the CLI and MCP resources.
- MCP search can find synced PRs and issues by free text or structured references like `owner/repo#123`.
