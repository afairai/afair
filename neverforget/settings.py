"""Runtime configuration — validated at boot.

Per global rules: fail fast on missing or malformed env. Parse, don't cast.
Per Invariant I5: model selection is env-driven and provider-agnostic.
Per Invariant I4: the vault directory is user-controlled.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide configuration loaded from environment + .env file."""

    model_config = SettingsConfigDict(
        # Load .env first, then .env.local — .env.local takes precedence so
        # developer-specific secrets override committed defaults. Same
        # convention as Next.js / Vite / Create-React-App.
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Runtime
    environment: Literal["local", "fly"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARN", "ERROR"] = "INFO"

    # ── Substrate
    vault_dir: Path = Field(
        default=Path.home() / "vault",
        description=(
            "Where the append-only substrate lives on disk. "
            "Local default: ~/vault. On Fly: /data/vault (mounted volume). "
            "User-controlled per Invariant I4."
        ),
    )

    # ── Substrate inline-vs-spill threshold
    inline_text_max_bytes: int = Field(
        default=64 * 1024,
        ge=1,
        description=(
            "Text payloads larger than this spill to the filesystem object "
            "store; smaller stays inline in the SQLite row. Binary content "
            "always spills regardless of size."
        ),
    )

    # ── MCP server
    mcp_host: str = "127.0.0.1"
    mcp_port: int = Field(default=8765, ge=1, le=65535)

    # ── LLM provider (Invariant I5 — vendor-neutral)
    # Format: "<provider>/<model>" — any litellm-supported model.
    extractor_model: str = "anthropic/claude-haiku-4-5"

    # Provider keys — set whichever your selected model needs.
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None

    # ── Embeddings
    embedding_model: str = "anthropic/voyage-3-lite"

    @field_validator("vault_dir", mode="before")
    @classmethod
    def _expand_user(cls, v: str | Path) -> Path:
        """Expand ~ in vault_dir so VAULT_DIR=~/vault works in env files."""
        return Path(str(v)).expanduser()

    @field_validator("extractor_model")
    @classmethod
    def _model_has_provider(cls, v: str) -> str:
        if "/" not in v:
            msg = (
                f"EXTRACTOR_MODEL must be in '<provider>/<model>' form "
                f"(e.g. 'anthropic/claude-haiku-4-5'); got {v!r}"
            )
            raise ValueError(msg)
        return v


def load_settings() -> Settings:
    """Load and validate settings. Raises on missing/malformed env."""
    return Settings()
