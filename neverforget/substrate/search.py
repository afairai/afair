"""Substrate search — FTS5 keyword retrieval over the immutable log.

Phase 0 uses FTS5 only. Vector search via sqlite-vec lives in the
Interpretation layer (task #4) and is composed on top of this.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .events import Event, row_to_event

if TYPE_CHECKING:
    import sqlite3


# FTS5 special characters that need to be stripped from natural-language
# queries before being passed as a MATCH expression. Hyphens are the most
# common gotcha — they parse as the NOT operator, so "smoke-test" tries to
# search for "smoke" NOT "test" and SQLite reports "no such column: test".
_FTS5_SPECIALS_RE = re.compile(r'[-+*"():^]')


def _safe_fts_query(query: str) -> str:
    """Convert a natural-language query into an FTS5-safe OR-of-tokens form.

    The recall tool's contract says "plain words, no special syntax". This
    helper honors that contract:
      - FTS5 special chars (- + * " ( ) : ^) are replaced with spaces
      - The result is split into tokens
      - Each token is double-quoted (FTS5 phrase syntax for a single word)
      - Tokens are joined with OR — FTS5 then ranks results by relevance
        (documents matching MORE tokens rank higher; documents matching
        ANY token still appear)

    Why OR + rank, not AND: a 4-token natural-language query like
    "cross-vendor verification I5 neutrality" should still find a document
    that contains "cross-vendor" but not "I5" — that document is clearly
    relevant. FTS5's BM25 ranking puts the strongest matches first; the
    LIMIT cuts off the tail.

    Returns an empty string when the query has no tokens — callers should
    short-circuit on that to avoid running an empty MATCH.
    """
    sanitized = _FTS5_SPECIALS_RE.sub(" ", query)
    tokens = [t for t in sanitized.split() if t]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)


def search_fts(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
) -> list[Event]:
    """Run an FTS5 ``MATCH`` query, return events ordered by rank.

    Natural-language queries are sanitized (see ``_safe_fts_query``) so
    callers can pass arbitrary text without worrying about FTS5 operator
    characters. Empty or all-stripped queries return an empty list.
    """
    safe = _safe_fts_query(query)
    if not safe:
        return []
    rows = conn.execute(
        """
        SELECT events.* FROM events
        JOIN events_fts ON events_fts.content_hash = events.content_hash
        WHERE events_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (safe, limit),
    ).fetchall()
    return [row_to_event(r) for r in rows]
