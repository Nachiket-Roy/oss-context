# oss-context

`oss-context` tracks pull request review decisions and reviewer state across GitHub PRs using a local SQLite knowledge graph.

This repository now contains the Phase 0 and Phase 1 foundation:

- GitHub sync into SQLite with incremental PR discovery
- Review thread + comment persistence
- Decision extraction with LLM providers or heuristic fallback
- Query-oriented CLI with rich terminal output
- Basic CI for linting and tests

## Status

Implemented in this branch:

- Phase 0: project scaffolding, schema, GitHub sync engine, CLI MVP
- Phase 1: decision extraction, context assembly, health summaries, rich output

Not yet implemented:

- Phase 2 MCP server / IDE integration
- Phase 3+ dashboard, hooks, notifications

## Installation

### With `uv`

```bash
uv sync --extra dev
uv run oss-context --help
```

### With `pip`

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
oss-context --help
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
```

## Notes

- The SQLite database runs in WAL mode so sync and query operations can safely overlap.
- Review thread state comes from GitHub review threads via GraphQL.
- Decision extraction is cached per comment body hash to avoid repeat analysis cost.
- `oss-context serve` is reserved for the Phase 2 MCP server and currently returns a status message.
