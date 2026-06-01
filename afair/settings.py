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

    # Vision model for image extraction — must be vision-capable.
    # Defaults to Haiku 4.5 (cheap, fast, has vision); upgrade to a
    # Sonnet tier when image understanding requirements grow.
    vision_model: str = "anthropic/claude-haiku-4-5"

    # Audio transcription model. litellm shape: ``openai/whisper-1``,
    # ``deepgram/nova-3``, ``groq/whisper-large-v3``. Vendor-neutral
    # via the model string.
    transcription_model: str = "openai/whisper-1"

    # Provider keys — set whichever your selected model needs. Adding a
    # provider here + a model-prefix branch in handlers._api_key_for_embedding
    # is the only code change to switch embedding/LLM vendors (I5).
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    # Voyage AI = Anthropic's blessed embedding provider (Anthropic doesn't
    # ship native embeddings). Set this to use ``voyage/voyage-3`` etc. for
    # the EMBEDDING_MODEL — keeps the whole RAG stack Anthropic-ecosystem
    # adjacent without depending on OpenAI for both LLM + embedding.
    voyage_api_key: SecretStr | None = None

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
            "AFAIR_AUTH_TOKEN",
            "afair_auth_token",
        ),
    )

    # — Scoped signup token (Security audit I7)
    # The afair-web landing page writes early-access signups into the
    # vault. Giving it the full bearer (which can read+write+observe
    # anything) means web-app compromise = full vault compromise.
    # This token is accepted ONLY on /internal/signup, which writes
    # one specific kind of event and nothing else. Web has just this.
    signup_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "signup_token",
            "AFAIR_SIGNUP_TOKEN",
            "afair_signup_token",
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
            "AFAIR_JWT_SECRET",
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
    # "hub" = federated GitHub OAuth through afair.ai (identity hub).
    # "github" = LEGACY direct GitHub OAuth dance (deprecated 2026-06-01,
    #            kept for fallback during the cutover).
    # Future: "magic-link", "clerk", "static-password".
    identity_backend: Literal["hub", "github"] = "hub"

    # — Identity hub (federated GitHub OAuth). Used when
    # identity_backend="hub" (the new default). The hub lives at
    # https://afair.ai/oauth/identity/* and issues signed JWTs that
    # this server verifies with the shared HMAC secret. See
    # afair/mcp/oauth/identity_hub.py.
    identity_hub_url: str = "https://afair.ai"
    identity_hub_secret: SecretStr | None = None

    # — LEGACY GitHub OAuth credentials. Only used when
    # identity_backend="github". Will be removed once the hub
    # cutover is verified across all deployments.
    github_oauth_client_id: SecretStr | None = None
    github_oauth_client_secret: SecretStr | None = None

    # — Allowlist of authenticated identities. Single-tenant per I8 — only
    # ONE GitHub login is allowed to authenticate against any given
    # instance. Comma-separated GitHub usernames (case-insensitive).
    identity_allowlist: str = ""

    # ── Vault encryption (Stufe 1)
    # Per-vault encryption key. When set, the SQLite database is opened
    # via SQLCipher (whole-file AES-256), and filesystem blobs are
    # written via AES-256-GCM (per-blob random nonce in the file header).
    # When unset, the substrate runs in plaintext mode — fine for local
    # dev, REQUIRED to be set in production (validated below).
    #
    # Generation: ``python -c 'import secrets; print(secrets.token_urlsafe(32))'``
    # gives a 32-byte random key suitable for both SQLCipher key derivation
    # and AES-256-GCM. Once a vault has been written with a key, losing
    # the key means losing the data — there is no recovery path.
    vault_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "vault_key",
            "AFAIR_VAULT_KEY",
            "afair_vault_key",
        ),
    )

    # ── Embeddings (Phase 1 — semantic recall via sqlite-vec)
    # Default: OpenAI text-embedding-3-small. We already have OPENAI_API_KEY,
    # 1536 dimensions, ~$0.02 per 1M tokens. Pluggable via litellm's standard
    # provider-prefix format. Tested providers + recommended dimensions:
    #   openai/text-embedding-3-small  → 1536 (default)
    #   openai/text-embedding-3-large  → 3072
    #   voyage/voyage-3                → 1024
    #   voyage/voyage-3-lite           → 512
    #   gemini/embedding-001           → 768
    #   cohere/embed-english-v3.0      → 1024
    # Any litellm-supported embedding model works — the only code change
    # needed to add a new provider is its API key field above.
    embedding_model: str = "openai/text-embedding-3-small"
    # Embedding vector dimension. MUST match the model (see table above).
    # The events_vec virtual table is created with this dimension at boot;
    # changing it requires dropping the vec table and re-embedding.
    embedding_dim: int = Field(default=1536, ge=64, le=8192)
    # Whether semantic recall is active. Disable to fall back to FTS-only
    # (useful for local dev without an OpenAI key).
    semantic_recall_enabled: bool = True

    # Phase 3 — Sleep Swarm cold-path workers. When True (default), the
    # scheduler runs Pruner + Conflict-Resolver + Consolidator on their
    # configured intervals. Disable for local dev where you don't want
    # background LLM calls to run unattended.
    cold_path_enabled: bool = True

    # Phase 4 Track 2 — per-hit surprise score uses entity-novelty against
    # the user's "recent context window": the last N events' canonical
    # entities. A hit with all-familiar entities scores 0.0, all-novel
    # scores 1.0. 20 is a starting point — wide enough to capture the
    # last working session, narrow enough that the comparison stays
    # responsive to context-switches.
    surprise_context_window: int = Field(default=20, ge=1, le=500)

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
                "AFAIR_AUTH_TOKEN must be set when ENVIRONMENT=fly. "
                "Generate one with: python -c "
                "'import secrets; print(secrets.token_urlsafe(32))'"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _vault_key_required_in_prod(self) -> Settings:
        """Fail boot if production environment lacks a vault encryption key.

        Without a vault key, the substrate runs in plaintext mode. That is
        fine for local dev but unacceptable for production: a stolen
        volume snapshot, an exfiltrated SQLite file, or a curious
        operator could read the data with zero effort. The encryption
        layer is what makes those scenarios non-events.

        The key is one-shot per vault: once data has been written under
        a given key, that key is the only way to read it. Losing it
        means losing the data. The .env.secrets.backup convention is
        the canonical recovery path; see docs/operations.md.
        """
        if self.environment == "fly" and self.vault_key is None:
            msg = (
                "AFAIR_VAULT_KEY must be set when ENVIRONMENT=fly. "
                "Generate one with: python -c "
                "'import secrets; print(secrets.token_urlsafe(32))'. "
                "Persist in Fly secret + .env.secrets.backup. "
                "Losing it = losing the vault."
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _oauth_issuer_required_in_prod(self) -> Settings:
        """Fail boot if production environment lacks an explicit OAuth issuer.

        The issuer URL is embedded in every JWT we mint AND in the
        well-known metadata that MCP clients use to discover the auth
        server. A wrong issuer silently breaks every OAuth handshake.
        The OLD code fell back to a hardcoded "https://afair.fly.dev"
        which was the dev URL — production runs at mcp.afair.ai and the
        mismatch was a silent footgun (Sec audit M1).
        """
        if self.environment == "fly" and not self.oauth_issuer:
            msg = (
                "OAUTH_ISSUER must be set when ENVIRONMENT=fly "
                "(e.g. https://mcp.afair.ai). Used as the `iss` claim "
                "in JWTs and the `issuer` field in OAuth metadata."
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

        Resolution order:
          1. Explicit ``OAUTH_ISSUER`` env var when set — wins always.
          2. Local dev: derives ``http://<host>:<port>`` from settings.
          3. Production (``environment="fly"``): the prod-boot validator
             below refuses to start without an explicit issuer, so
             this property never sees that case. The explicit failure
             beats a silent fallback to a wrong-domain URL.
        """
        if self.oauth_issuer:
            return self.oauth_issuer.rstrip("/")
        return f"http://{self.mcp_host}:{self.mcp_port}"


def load_settings() -> Settings:
    """Load and validate settings. Raises on missing/malformed env."""
    return Settings()
