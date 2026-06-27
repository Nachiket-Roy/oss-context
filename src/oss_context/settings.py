from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

LLMProvider = Literal["heuristic", "openai", "anthropic"]


class Settings(BaseModel):
    db_path: Path
    github_api_url: str = "https://api.github.com"
    github_graphql_url: str = "https://api.github.com/graphql"
    github_token: str | None = None
    llm_provider: LLMProvider = "heuristic"
    llm_api_key: str | None = None
    llm_model: str = Field(default="heuristic-v1")
    request_timeout_seconds: float = 30.0
    response_cache_ttl_seconds: int = 60


def _default_db_path() -> Path:
    xdg_data_home = os.getenv("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / "oss-context" / "oss-context.db"
    return Path.home() / ".local" / "share" / "oss-context" / "oss-context.db"


def _auto_provider() -> LLMProvider:
    explicit = os.getenv("OSS_CONTEXT_LLM_PROVIDER")
    if explicit is not None:
        normalized = explicit.strip().lower()
        if normalized == "heuristic":
            return "heuristic"
        if normalized == "openai":
            return "openai"
        if normalized == "anthropic":
            return "anthropic"
        raise ValueError("OSS_CONTEXT_LLM_PROVIDER must be one of: heuristic, openai, anthropic")
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "heuristic"


def _default_model(provider: LLMProvider) -> str:
    return {
        "heuristic": "heuristic-v1",
        "openai": "gpt-4.1-mini",
        "anthropic": "claude-3-5-sonnet-latest",
    }[provider]


def load_settings(db_path: Path | None = None) -> Settings:
    provider = _auto_provider()
    api_key = os.getenv("OSS_CONTEXT_LLM_API_KEY")
    if not api_key:
        if provider == "openai":
            api_key = os.getenv("OPENAI_API_KEY")
        elif provider == "anthropic":
            api_key = os.getenv("ANTHROPIC_API_KEY")

    if provider in {"openai", "anthropic"} and not api_key:
        raise ValueError(
            f"{provider} provider selected but no API key was configured. "
            + "Set OSS_CONTEXT_LLM_API_KEY or the provider-specific API key env var."
        )

    resolved_db_path = db_path or Path(os.getenv("OSS_CONTEXT_DB_PATH", _default_db_path()))
    return Settings(
        db_path=resolved_db_path,
        github_token=os.getenv("GITHUB_TOKEN"),
        llm_provider=provider,
        llm_api_key=api_key,
        llm_model=os.getenv("OSS_CONTEXT_LLM_MODEL", _default_model(provider)),
    )
