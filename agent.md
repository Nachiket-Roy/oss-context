# agent.md

This file is a short guide for coding agents and contributors working in this repository.

## What this repository does

`oss-context` syncs GitHub pull-request and issue state into SQLite, extracts decision signals and structured references from bodies/comments, exposes cross-repo dashboard summaries, builds a lightweight local code index, and serves that context through a CLI, an MCP server, a local HTML UI, and branch-aware git workflows.

## Repository layout

- `src/oss_context/` — application source code
- `tests/` — regression and behavior tests
- `.github/workflows/ci.yml` — CI pipeline for lint, type checking, and tests
- `pyproject.toml` — packaging, dependencies, and tool configuration
- `README.md` — user-facing setup and usage guide
- `src/oss_context/mcp_server.py` — FastMCP server tools and resources
- `src/oss_context/markdown.py` — markdown assembly for MCP responses
- `src/oss_context/web_ui.py` — local HTML dashboard and detail pages
- `src/oss_context/branch_context.py` — git branch to PR resolution and file-level branch context
- `src/oss_context/code_index.py` — local Python code indexing, symbol search, and file context
- `src/oss_context/review_assistant.py` — merge-readiness summaries and follow-up guidance
- `src/oss_context/retrieval.py` — provenance helpers and retrieval diagnostics
- `src/oss_context/hooks.py` — warning-only git hook installation helpers
- `future_work.md` — planned future roadmap items

## File documentation convention

Each maintained source, test, and configuration file should start with a short header comment or module docstring describing:

1. what the file is responsible for
2. how it fits into the rest of the project
3. any important development or maintenance context

For Python modules, prefer top-level module docstrings.
For YAML, TOML, and ignore/config files, prefer concise leading comments when the format allows them.

## Local development workflow

Install dependencies:

```bash
uv sync --extra dev
```

Run the full CI-equivalent command set locally:

```bash
uv run ruff check .
uv run pyright
uv run pytest
```

Or as a single command:

```bash
uv sync --extra dev && uv run ruff check . && uv run pyright && uv run pytest
```

## Expectations for future changes

- Keep edits focused and minimal.
- Prefer adding tests alongside behavior changes.
- Keep local commands aligned with `.github/workflows/ci.yml`.
- Keep MCP tools/resources and CLI query behavior consistent when they expose the same underlying data.
- Prefer deterministic retrieval paths before looser matching: exact branch links, exact file matches, explicit GitHub references, then symbol relationships.
- When returning context items, include provenance, confidence, and a concrete retrieval reason whenever the surface supports it.
- Do not edit generated files unless regeneration is intentional.
