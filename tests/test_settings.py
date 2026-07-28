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


def test_max_tool_threads_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    """P2a: the thread-limiter cap must be > 0 (a zero would deadlock every
    tool call)."""
    monkeypatch.setenv("MAX_TOOL_THREADS", "0")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


# ── blank-secret normalization ───────────────────────────────────────────────
# A present-but-empty secret env var (the shape .env.example ships,
# e.g. `AFAIR_VAULT_KEY=`) parses as "" not None. Two footguns followed before
# the _blank_secret_is_unset normalizer: empty AFAIR_VAULT_KEY crashed LOCAL
# boot on the length check, and empty AFAIR_AUTH_TOKEN slipped past the fly
# required-gate and booted the public server unauthenticated.

# A valid 32+ byte key for the fly-mode tests (token_urlsafe(32)-shaped).
_VALID_KEY = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"  # 40 bytes


def test_blank_vault_key_boots_local(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`AFAIR_VAULT_KEY=` (the .env.example default) must not crash local boot.

    Regression: pre-fix this raised "AFAIR_VAULT_KEY is too short" because ""
    is not None, so the length check fired even in plaintext-OK local mode.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("AFAIR_VAULT_KEY", "")
    monkeypatch.setenv("AFAIR_AUTH_TOKEN", "")

    s = Settings(_env_file=None)  # type: ignore[call-arg]

    assert s.vault_key is None  # blank → unset → plaintext local
    assert s.auth_token is None


def test_blank_vault_key_normalizes_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whitespace-only is also treated as unset, not as a 3-byte key."""
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("AFAIR_VAULT_KEY", "   ")

    assert Settings(_env_file=None).vault_key is None  # type: ignore[call-arg]


def test_blank_auth_token_rejected_in_fly(monkeypatch: pytest.MonkeyPatch) -> None:
    """The security fix: an empty AFAIR_AUTH_TOKEN must NOT boot a fly server.

    Pre-fix, "" passed the `auth_token is None` gate and the public server
    came up world-readable/writable. Blank → None makes the gate fire.
    """
    monkeypatch.setenv("ENVIRONMENT", "fly")
    monkeypatch.setenv("AFAIR_AUTH_TOKEN", "")
    monkeypatch.setenv("AFAIR_VAULT_KEY", _VALID_KEY)
    monkeypatch.setenv("OAUTH_ISSUER", "https://memory.example.com")

    with pytest.raises(ValidationError, match="AFAIR_AUTH_TOKEN"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_blank_vault_key_rejected_in_fly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank vault key in fly must fail loudly (not silently run plaintext)."""
    monkeypatch.setenv("ENVIRONMENT", "fly")
    monkeypatch.setenv("AFAIR_AUTH_TOKEN", _VALID_KEY)
    monkeypatch.setenv("AFAIR_VAULT_KEY", "")
    monkeypatch.setenv("OAUTH_ISSUER", "https://memory.example.com")

    with pytest.raises(ValidationError, match="AFAIR_VAULT_KEY"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_short_nonempty_vault_key_still_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """The length check is preserved for a real-but-weak key (Security L3)."""
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("AFAIR_VAULT_KEY", "short")

    with pytest.raises(ValidationError, match="too short"):
        Settings(_env_file=None)  # type: ignore[call-arg]


# ── per-agent model overrides (VISION §6.5 — heterogeneous models per agent) ──
# Each cold-path LLM worker reads its own model field; unset overrides resolve
# to extractor_model at boot so the single-model behavior is preserved
# byte-identically unless an operator configures a per-agent model.

_PER_AGENT_MODEL_FIELDS = (
    "canonicalizer_model",
    "entity_dedup_model",
    "conflict_resolver_model",
    "consolidator_model",
    "entity_articles_model",
    "living_syntheses_model",
    "temporal_model",
    "schema_evolver_model",
)

_PER_AGENT_ENV_VARS = (*(f.upper() for f in _PER_AGENT_MODEL_FIELDS), "JUDGE_PANEL")


def _clear_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("EXTRACTOR_MODEL", *_PER_AGENT_ENV_VARS):
        monkeypatch.delenv(key, raising=False)


def test_per_agent_models_default_to_extractor_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset overrides → every worker model resolves to extractor_model.

    Behavior-preservation guarantee: with nothing configured, the
    heterogeneous-models feature is invisible and every cold-path worker
    runs the exact model it ran before the feature existed.
    """
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("EXTRACTOR_MODEL", "openai/gpt-4o-mini")

    s = Settings(_env_file=None)  # type: ignore[call-arg]

    for field in _PER_AGENT_MODEL_FIELDS:
        assert getattr(s, field) == "openai/gpt-4o-mini", field


def test_single_agent_override_leaves_others_on_extractor_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONSOLIDATOR_MODEL override applies to the consolidator only."""
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("EXTRACTOR_MODEL", "anthropic/claude-haiku-4-5")
    monkeypatch.setenv("CONSOLIDATOR_MODEL", "anthropic/claude-sonnet-4-5")

    s = Settings(_env_file=None)  # type: ignore[call-arg]

    assert s.consolidator_model == "anthropic/claude-sonnet-4-5"
    for field in _PER_AGENT_MODEL_FIELDS:
        if field == "consolidator_model":
            continue
        assert getattr(s, field) == "anthropic/claude-haiku-4-5", field


def test_blank_agent_override_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Present-but-empty override (the .env.example shape) → extractor_model."""
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("EXTRACTOR_MODEL", "anthropic/claude-haiku-4-5")
    monkeypatch.setenv("CANONICALIZER_MODEL", "  ")

    s = Settings(_env_file=None)  # type: ignore[call-arg]

    assert s.canonicalizer_model == "anthropic/claude-haiku-4-5"


def test_agent_override_requires_provider_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invariant I5 enforcement — an override without a provider is rejected."""
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("CONFLICT_RESOLVER_MODEL", "claude-sonnet-4-5")

    with pytest.raises(ValidationError, match="CONFLICT_RESOLVER_MODEL"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_extractor_model_override_cascades_to_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single EXTRACTOR_MODEL change moves every non-overridden agent with it."""
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("EXTRACTOR_MODEL", "ollama/qwen2.5:7b")

    s = Settings(_env_file=None)  # type: ignore[call-arg]

    assert s.temporal_model == "ollama/qwen2.5:7b"
    assert s.entity_articles_model == "ollama/qwen2.5:7b"
    assert s.living_syntheses_model == "ollama/qwen2.5:7b"


# ── judge panel configuration ─────────────────────────────────────────────────


def test_judge_panel_defaults_to_builtin_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset JUDGE_PANEL → the built-in three-vendor DEFAULT_PANEL."""
    from afair.agents.llm_judge import DEFAULT_PANEL

    _clear_model_env(monkeypatch)

    s = Settings(_env_file=None)  # type: ignore[call-arg]

    assert s.judge_panel_models == DEFAULT_PANEL


def test_judge_panel_override_parses_comma_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """JUDGE_PANEL env var replaces the default panel, whitespace-tolerant."""
    _clear_model_env(monkeypatch)
    monkeypatch.setenv(
        "JUDGE_PANEL",
        "anthropic/claude-opus-4-5, openai/gpt-5 ,gemini/gemini-2.5-pro",
    )

    s = Settings(_env_file=None)  # type: ignore[call-arg]

    assert s.judge_panel_models == (
        "anthropic/claude-opus-4-5",
        "openai/gpt-5",
        "gemini/gemini-2.5-pro",
    )


def test_judge_panel_entry_requires_provider_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invariant I5 — every panel entry needs the '<provider>/<model>' form."""
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("JUDGE_PANEL", "anthropic/claude-sonnet-4-5,gpt-5")

    with pytest.raises(ValidationError, match="JUDGE_PANEL"):
        Settings(_env_file=None)  # type: ignore[call-arg]


# ── principal (ADR-0010) ─────────────────────────────────────────────────────


def test_principal_defaults_to_person() -> None:
    """Default principal is a person with a blank name (byte-identity path)."""
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.principal_kind == "person"
    assert s.principal_name == ""


def test_person_ignores_supplied_name() -> None:
    """A person principal drops any supplied name — v1 byte-identity: the name
    can never reach a rendered prompt."""
    s = Settings(_env_file=None, principal_kind="person", principal_name="Ignored Inc")  # type: ignore[call-arg]
    assert s.principal_name == ""


def test_organization_requires_name() -> None:
    """An organization principal without a name fails boot loudly."""
    with pytest.raises(ValidationError, match="PRINCIPAL_NAME"):
        Settings(_env_file=None, principal_kind="organization")  # type: ignore[call-arg]


def test_organization_blank_name_rejected() -> None:
    """A whitespace/control-only org name sanitizes to blank and is rejected."""
    with pytest.raises(ValidationError, match="PRINCIPAL_NAME"):
        Settings(_env_file=None, principal_kind="organization", principal_name="   \n\t  ")  # type: ignore[call-arg]


def test_organization_name_sanitized() -> None:
    """Newlines/control chars collapse to single spaces; the name is trimmed and
    capped so it can't reshape an interpolated system prompt."""
    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        principal_kind="organization",
        principal_name="  Acme\n\tCorp  ",
    )
    assert s.principal_name == "Acme Corp"


def test_organization_name_capped() -> None:
    """An absurdly long org name is capped (interpolated into prompts)."""
    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        principal_kind="organization",
        principal_name="A" * 500,
    )
    assert len(s.principal_name) == 120


def test_invalid_principal_kind_rejected() -> None:
    """Only 'person' | 'organization' are valid principal kinds."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, principal_kind="team")  # type: ignore[call-arg]


def test_bare_principal_env_binds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bare ``PRINCIPAL_KIND`` / ``PRINCIPAL_NAME`` names bind (case-insensitive)."""
    monkeypatch.setenv("PRINCIPAL_KIND", "organization")
    monkeypatch.setenv("PRINCIPAL_NAME", "Globex")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.principal_kind == "organization"
    assert s.principal_name == "Globex"


def test_afair_prefixed_principal_env_binds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The DOCUMENTED ``AFAIR_PRINCIPAL_KIND`` / ``AFAIR_PRINCIPAL_NAME`` env vars
    bind via AliasChoices, exactly like AFAIR_VAULT_KEY / AFAIR_AUTH_TOKEN. Without
    the alias only the bare name bound, so the documented var silently did nothing
    and an org vault booted in person mode. Regression guard for that."""
    monkeypatch.setenv("AFAIR_PRINCIPAL_KIND", "organization")
    monkeypatch.setenv("AFAIR_PRINCIPAL_NAME", "Acme Corp")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.principal_kind == "organization"
    assert s.principal_name == "Acme Corp"
