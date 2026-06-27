"""Module entry point for `python -m oss_context`.

This file keeps the package executable by delegating process startup to the
Typer application defined in `oss_context.cli`, including the Phase 2 MCP
server entrypoint exposed through the `serve` command.
"""

from oss_context.cli import app

app()
