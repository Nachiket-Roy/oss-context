"""Tests for environment-driven settings resolution.

This file verifies provider validation, credential requirements, and default
heuristic behavior so configuration mistakes fail early and predictably.
"""

from pathlib import Path

import pytest

from oss_context.settings import load_settings


@pytest.fixture(autouse=True)
def clear_provider_env(monkeypatch):
    for key in [
        "OSS_CONTEXT_LLM_PROVIDER",
        "OSS_CONTEXT_LLM_API_KEY",
        "OSS_CONTEXT_LLM_MODEL",
        "OSS_CONTEXT_DB_PATH",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GITHUB_TOKEN",
    ]:
        monkeypatch.delenv(key, raising=False)


def test_load_settings_rejects_unknown_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("OSS_CONTEXT_LLM_PROVIDER", "claudee")
    with pytest.raises(ValueError, match="OSS_CONTEXT_LLM_PROVIDER"):
        load_settings(tmp_path / "db.sqlite")


def test_load_settings_requires_remote_provider_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("OSS_CONTEXT_LLM_PROVIDER", "openai")
    with pytest.raises(ValueError, match="no API key"):
        load_settings(tmp_path / "db.sqlite")


def test_load_settings_accepts_heuristic_without_api_key(tmp_path):
    settings = load_settings(Path(tmp_path) / "db.sqlite")
    assert settings.llm_provider == "heuristic"
    assert settings.llm_api_key is None
