# oss-context

`oss-context` tracks pull request review decisions and reviewer state across GitHub PRs using a local SQLite knowledge graph.

This repository now contains the Phase 0 through Phase 3 core foundation:

- GitHub sync into SQLite with incremental PR discovery
- Review thread + comment persistence
- Decision extraction with LLM providers or heuristic fallback
- Query-oriented CLI with rich terminal output
- MCP server integration for IDE and agent workflows
- Cross-repo dashboard and reviewer-load queries
- Basic CI for linting and tests

## Status

Implemented in this branch:

- Phase 0: project scaffolding, schema, GitHub sync engine, CLI MVP
- Phase 1: decision extraction, context assembly, health summaries, rich output
- Phase 2: MCP server with tools and resources for PR context and unresolved state
- Phase 3: cross-repo queries, reviewer-status summaries, and dashboard metrics

Not yet implemented:

- Optional Phase 3 web dashboard
- Phase 4+ hooks, notifications, and branch-context integration

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

If no LLM provider is configured, `oss-context` falls back to a deterministic heuristic classifier so Phase 1 remains usable without secrets.

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

# Show PR health and reviewer state
oss-context query --repo owner/repo --pr 42 --health

# Show a cross-repo dashboard
oss-context query --dashboard

# Show reviewer-specific pending work across repos
oss-context query --reviewer bob --pending

# Show tracked repository sync status
oss-context query --repos
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

Exposed MCP tools include:

- `sync_repo`
- `get_pr_context`
- `get_unresolved_threads`
- `get_reviewer_state`
- `get_dashboard`

Exposed MCP resources include:

- `pr://{owner}/{name}/{pr_number}/context`
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
- Decision extraction is cached per comment body hash to avoid repeat analysis cost.
- `oss-context serve` now starts the FastMCP server for IDE and agent integrations.
- Cross-repo dashboard queries are available directly from the CLI and MCP resources.
