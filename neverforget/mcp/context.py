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


@dataclass
class ServerContext:
    """Everything a tool handler needs to talk to the substrate."""

    db: sqlite3.Connection
    vault_dir: Path
    inline_text_max_bytes: int


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
