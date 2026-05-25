"""Substrate search — FTS5 keyword retrieval over the immutable log.

Phase 0 uses FTS5 only. Vector search via sqlite-vec lives in the
Interpretation layer (task #4) and is composed on top of this.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .events import Event, row_to_event

if TYPE_CHECKING:
    import sqlite3


def search_fts(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
) -> list[Event]:
    """Run an FTS5 ``MATCH`` query, return events ordered by rank.

    The ``query`` is passed directly to SQLite's FTS5 query syntax — quotes
    for phrase match, ``NEAR()``, ``OR``, ``-`` for exclude, all supported.
    """
    rows = conn.execute(
        """
        SELECT events.* FROM events
        JOIN events_fts ON events_fts.content_hash = events.content_hash
        WHERE events_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (query, limit),
    ).fetchall()
    return [row_to_event(r) for r in rows]
