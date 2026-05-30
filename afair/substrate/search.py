"""Substrate search — FTS5 keyword retrieval + sqlite-vec semantic recall.

Phase 0 was FTS-only. Phase 1 adds vector recall via sqlite-vec and a
hybrid merge using Reciprocal Rank Fusion. Both paths return ``Event``
objects from the same substrate table.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .events import Event, row_to_event

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence


# FTS5 special characters that need to be stripped from natural-language
# queries before being passed as a MATCH expression. Hyphens are the most
# common gotcha — they parse as the NOT operator, so "smoke-test" tries to
# search for "smoke" NOT "test" and SQLite reports "no such column: test".
#
# Public so other modules (e.g. depth-routing in handlers.py) can reuse
# the precompiled pattern instead of re-compiling their own copy.
FTS5_SPECIALS_RE = re.compile(r'[-+*"():^]')
_FTS5_SPECIALS_RE = FTS5_SPECIALS_RE  # internal alias for back-compat


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


def search_vec(
    conn: sqlite3.Connection,
    query_vector: Sequence[float],
    *,
    limit: int = 20,
) -> list[Event]:
    """Run a cosine-similarity vector query against events_vec.

    Returns events ordered by closest distance (smaller = more similar).
    Events that have no embedding row (e.g., extraction failed) don't
    appear here — they remain reachable via FTS.
    """
    import struct

    payload = struct.pack(f"<{len(query_vector)}f", *query_vector)
    rows = conn.execute(
        """
        SELECT events.* FROM events_vec
        JOIN events ON events.content_hash = events_vec.content_hash
        WHERE embedding MATCH ? AND k = ?
        ORDER BY distance
        """,
        (payload, limit),
    ).fetchall()
    return [row_to_event(r) for r in rows]


def rrf_merge(
    fts_hits: list[Event],
    vec_hits: list[Event],
    *,
    limit: int = 20,
    rrf_k: int = 60,
) -> list[Event]:
    """Pure merge function — combine two ranked result lists via RRF.

    Separated from ``hybrid_search`` so callers that fetched FTS and vec
    results in parallel (e.g., recall with the embedding API call running
    concurrently with FTS) can merge without re-running the queries.
    """
    if not fts_hits and not vec_hits:
        return []
    if not vec_hits:
        return fts_hits[:limit]
    if not fts_hits:
        return vec_hits[:limit]

    scores: dict[str, float] = {}
    by_id: dict[str, Event] = {}
    for rank, event in enumerate(fts_hits):
        scores[event.id] = scores.get(event.id, 0.0) + 1.0 / (rrf_k + rank + 1)
        by_id.setdefault(event.id, event)
    for rank, event in enumerate(vec_hits):
        scores[event.id] = scores.get(event.id, 0.0) + 1.0 / (rrf_k + rank + 1)
        by_id.setdefault(event.id, event)

    sorted_ids = sorted(scores, key=scores.__getitem__, reverse=True)
    return [by_id[eid] for eid in sorted_ids[:limit]]


def hybrid_search(
    conn: sqlite3.Connection,
    *,
    query: str,
    query_vector: Sequence[float] | None,
    limit: int = 20,
    rrf_k: int = 60,
) -> list[Event]:
    """Combine FTS5 + vector results via Reciprocal Rank Fusion.

    Sequential variant — runs FTS, then vec, then merges. Callers that want
    to overlap the embedding API call with FTS should fetch the two
    result lists themselves (in parallel) and call ``rrf_merge`` directly.

    When ``query_vector`` is ``None`` this falls back to FTS-only —
    useful when semantic_recall is disabled or the embedding API failed.
    """
    fts_hits = search_fts(conn, query, limit=limit)
    vec_hits = search_vec(conn, query_vector, limit=limit) if query_vector is not None else []
    return rrf_merge(fts_hits, vec_hits, limit=limit, rrf_k=rrf_k)
