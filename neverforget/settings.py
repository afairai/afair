"""Runtime configuration — validated at boot.

Per global rules: fail fast on missing or malformed env. Parse, don't cast.
Per Invariant I5: model selection is env-driven and provider-agnostic.
Per Invariant I4: the vault directory is user-controlled.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _normalize_allowlist(raw: str) -> tuple[str, ...]:
    """Comma-separated allowlist → lowercase tuple. Empty entries dropped."""
    return tuple(s.strip().lower() for s in raw.split(",") if s.strip())


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

    # ── Authentication
    # Two layers, defense-in-depth:
    #   1. Static bearer token (`auth_token`) — convenience for CI smokes
    #      and server-to-server scripts.
    #   2. OAuth 2.1 (issued by US, identity-backed by GitHub) — for AI
    #      clients (Claude.ai requires this; Claude Code + Codex support both).
    # The MCP middleware accepts EITHER form on /mcp.

    # — Static bearer (Phase 0 mechanism, still supported)
    auth_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "auth_token",
            "NEVERFORGET_AUTH_TOKEN",
            "neverforget_auth_token",
        ),
    )

    # — OAuth server signing
    # We issue JWTs signed with this secret (HS256 for Phase 1; RS256
    # upgrade lives in a later phase if/when we need cross-instance
    # token verification).
    jwt_secret: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "jwt_secret",
            "NEVERFORGET_JWT_SECRET",
        ),
    )
    # Access token lifetime (1 hour by default; Authlib's standard).
    access_token_ttl_seconds: int = Field(default=3600, ge=60)
    # Refresh token lifetime (30 days).
    refresh_token_ttl_seconds: int = Field(default=30 * 24 * 3600, ge=60)

    # — OAuth issuer URL (used as the `iss` claim in JWTs we issue and
    # the `issuer` field in oauth-authorization-server metadata).
    # Defaults to the public URL of the server in prod; local dev falls
    # back to http://localhost:<port>.
    oauth_issuer: str | None = None

    # — Identity backend selection. Pluggable per-deployment.
    # "github" = OAuth dance with GitHub (Phase 1 default).
    # Future: "magic-link", "clerk", "static-password".
    identity_backend: Literal["github"] = "github"

    # — GitHub OAuth credentials (used when identity_backend="github").
    github_oauth_client_id: SecretStr | None = None
    github_oauth_client_secret: SecretStr | None = None

    # — Allowlist of authenticated identities. Single-tenant per I8 — only
    # ONE GitHub login is allowed to authenticate against any given
    # instance. Comma-separated GitHub usernames (case-insensitive).
    identity_allowlist: str = ""

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

    @model_validator(mode="after")
    def _auth_required_in_prod(self) -> Settings:
        """Fail boot if production environment lacks an auth token.

        The deployed server is publicly addressable. Without an auth token
        configured the substrate would be world-readable AND world-writable.
        Refuse to start so the misconfiguration is loud instead of silent.
        """
        if self.environment == "fly" and self.auth_token is None:
            msg = (
                "NEVERFORGET_AUTH_TOKEN must be set when ENVIRONMENT=fly. "
                "Generate one with: python -c "
                "'import secrets; print(secrets.token_urlsafe(32))'"
            )
            raise ValueError(msg)
        return self

    @property
    def allowlist(self) -> tuple[str, ...]:
        """Normalized lowercase allowlist (set of allowed GitHub usernames)."""
        return _normalize_allowlist(self.identity_allowlist)

    @property
    def effective_oauth_issuer(self) -> str:
        """Issuer URL for JWTs we mint.

        In prod (Fly) defaults to the public app URL. Locally, derives from
        host/port. Settable explicitly via OAUTH_ISSUER for custom-domain
        deployments.
        """
        if self.oauth_issuer:
            return self.oauth_issuer.rstrip("/")
        if self.environment == "fly":
            return "https://neverforget.fly.dev"
        return f"http://{self.mcp_host}:{self.mcp_port}"


def load_settings() -> Settings:
    """Load and validate settings. Raises on missing/malformed env."""
    return Settings()
