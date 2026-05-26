"""Smoke tests for the settings module — proves the scaffold loads end-to-end."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from afair.settings import Settings

if TYPE_CHECKING:
    from pathlib import Path


def test_defaults_load(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Settings load with defaults when no env is set."""
    # Isolate from the developer's real environment.
    for key in [
        "ENVIRONMENT",
        "LOG_LEVEL",
        "VAULT_DIR",
        "MCP_HOST",
        "MCP_PORT",
        "EXTRACTOR_MODEL",
        "EMBEDDING_MODEL",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
    ]:
        monkeypatch.delenv(key, raising=False)
    # Pretend $HOME points at a tmp dir so vault_dir is deterministic.
    monkeypatch.setenv("HOME", str(tmp_path))

    s = Settings(_env_file=None)  # type: ignore[call-arg]

    assert s.environment == "local"
    assert s.log_level == "INFO"
    assert s.mcp_host == "127.0.0.1"
    assert s.mcp_port == 8765
    assert s.extractor_model == "anthropic/claude-haiku-4-5"
    assert s.extractor_model.startswith("anthropic/")


def test_vault_dir_expands_tilde(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """VAULT_DIR=~/vault expands to an absolute path under the user's home."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VAULT_DIR", "~/vault")

    s = Settings(_env_file=None)  # type: ignore[call-arg]

    assert s.vault_dir.is_absolute()
    assert s.vault_dir == tmp_path / "vault"


def test_extractor_model_must_include_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invariant I5 enforcement at config time — provider must be explicit."""
    monkeypatch.setenv("EXTRACTOR_MODEL", "claude-haiku-4-5")  # no provider prefix

    with pytest.raises(ValidationError, match="provider"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_mcp_port_validates_range(monkeypatch: pytest.MonkeyPatch) -> None:
    """Out-of-range port rejected at boot."""
    monkeypatch.setenv("MCP_PORT", "999999")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]
