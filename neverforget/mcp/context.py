"""Server-side context — the substrate connection + settings.

A module-level singleton is fine for Phase 0 single-tenant per Invariant I8.
Refactoring to richer dependency injection is permitted by I7 because it
lives BELOW the MCP surface; the tool signatures don't change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from pydantic import SecretStr


@dataclass
class ServerContext:
    """Everything a tool handler or agent needs at runtime.

    Carries the substrate connection, the vault path, the inline-vs-spill
    threshold, the selected extractor model, and provider API keys. New
    fields may be added; existing fields are part of the v1 internal
    contract (handlers and agents rely on them).
    """

    db: sqlite3.Connection
    vault_dir: Path
    inline_text_max_bytes: int
    # Extractor / LLM (Invariant I5 — model string drives provider via litellm)
    extractor_model: str = "anthropic/claude-haiku-4-5"
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None


_context: ServerContext | None = None


def set_context(ctx: ServerContext) -> None:
    """Install the active context. Called once at server startup."""
    global _context
    _context = ctx


def get_context() -> ServerContext:
    """Retrieve the active context. Raises if uninitialized."""
    if _context is None:
        msg = "ServerContext not initialized — call set_context() first"
        raise RuntimeError(msg)
    return _context


def clear_context() -> None:
    """Used in tests for clean teardown between cases."""
    global _context
    _context = None
