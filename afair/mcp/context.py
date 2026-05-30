"""Server-side context — vault location, settings, and per-thread DB connection cache.

A module-level singleton is fine for Phase 0 single-tenant per Invariant I8.
Refactoring to richer dependency injection is permitted by I7 because it
lives BELOW the MCP surface; the tool signatures don't change.

DB connection model (since 2026-05-25):
  - ServerContext NO LONGER holds a single shared sqlite3.Connection in
    production. Instead, each MCP-handler thread gets its OWN connection
    via ``connect_for_thread()``, cached on threading.local. This unlocks
    SQLite WAL's concurrent-reader model — Claude Code + Claude.ai +
    Codex can hit the server simultaneously without serializing on a
    single Python sqlite3 mutex.
  - For backward compat in unit tests, ``ServerContext.db`` is kept as an
    OPTIONAL field. If a test pre-sets it, ``connect_for_thread()`` will
    return that connection and never open a new one. Production never
    sets ``db`` — see server.build_server().
"""

from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..substrate import open_db

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from pydantic import SecretStr


@dataclass
class ServerContext:
    """Everything a tool handler or agent needs at runtime.

    Carries the vault path, the inline-vs-spill threshold, the selected
    extractor model, and provider API keys. The DB connection is acquired
    lazily per-thread via ``connect_for_thread()`` — see module docstring.
    New fields may be added; existing fields are part of the v1 internal
    contract (handlers and agents rely on them).
    """

    vault_dir: Path
    inline_text_max_bytes: int
    # Extractor / LLM (Invariant I5 — model string drives provider via litellm)
    extractor_model: str = "anthropic/claude-haiku-4-5"
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    voyage_api_key: SecretStr | None = None
    # Embeddings (Phase 1 — semantic recall via sqlite-vec)
    embedding_model: str = "openai/text-embedding-3-small"
    embedding_dim: int = 1536
    semantic_recall_enabled: bool = True
    cold_path_enabled: bool = True
    # Phase 4 Track 2 — see Settings.surprise_context_window
    surprise_context_window: int = 20
    # Optional override — set ONLY in unit tests that want to share their
    # fixture's connection with handler calls. Production leaves this None
    # so connect_for_thread() opens per-thread connections.
    db: sqlite3.Connection | None = None


_context: ServerContext | None = None
_thread_local = threading.local()


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
    # Also drop any thread-local connections so the next test starts clean.
    if hasattr(_thread_local, "db"):
        with contextlib.suppress(Exception):
            _thread_local.db.close()
        delattr(_thread_local, "db")
    # Same for the extractor pool's thread-local connection (Perf I3 cache).
    # Tests reuse the same worker thread across tmp_paths, so a stale
    # cached connection would point at the previous test's tmp dir.
    from ..agents.extractor import clear_extractor_thread_db

    clear_extractor_thread_db()


def connect_for_thread() -> sqlite3.Connection:
    """Return a SQLite connection scoped to the calling thread.

    If the active context has a ``db`` set (test mode), return that
    connection directly — tests rely on direct-verification queries
    seeing the same writes the handler made.

    Otherwise lazily open a connection and cache it on the thread.
    uvicorn's threadpool reuses workers, so each worker pays the
    one-time open cost (~5-20ms) and then handles requests at full
    SQLite speed without per-request open/close churn.
    """
    ctx = get_context()
    if ctx.db is not None:
        return ctx.db
    conn = getattr(_thread_local, "db", None)
    if conn is None:
        conn = open_db(ctx.vault_dir, embedding_dim=ctx.embedding_dim)
        _thread_local.db = conn
    return conn
