# agent.md

This file is a short guide for coding agents and contributors working in this repository.

## What this repository does

`oss-context` syncs GitHub pull-request review state into SQLite, extracts decision signals from comments, and exposes that state through a CLI-oriented knowledge graph.

## Repository layout

- `src/oss_context/` — application source code
- `tests/` — regression and behavior tests
- `.github/workflows/ci.yml` — CI pipeline for lint, type checking, and tests
- `pyproject.toml` — packaging, dependencies, and tool configuration
- `README.md` — user-facing setup and usage guide

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
- Do not edit generated files unless regeneration is intentional.
