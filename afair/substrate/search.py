"""Substrate search — FTS5 keyword retrieval + sqlite-vec semantic recall.

Phase 0 was FTS-only. Phase 1 adds vector recall via sqlite-vec and a
hybrid merge using Reciprocal Rank Fusion. Both paths return ``Event``
objects from the same substrate table.

``mmr_rerank`` adds a diversity pass over an already-ranked list. The idea is
Maximal Marginal Relevance (Carbonell & Goldstein 1998); Graphiti (getzep,
Apache-2.0) serves it as one of its reranker options, which is where the
"a retrieval stack needs a redundancy penalty, not just a relevance score"
framing comes from. The implementation here is our own and deliberately
different: it is lexical and dependency-free, so it needs neither a second
embedding round-trip nor a cross-encoder, and it stays a pure function over
the substrate's own ``Event`` objects.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING

from .events import Event, row_to_event

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Sequence


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

    Recall fetches the FTS and vec result lists in parallel (the embedding
    API call runs concurrently with FTS) and calls this to merge them without
    re-running the queries.
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


# ── MMR diversity rerank ─────────────────────────────────────────────────────
# A vault that is written to by daily jobs accumulates near-duplicates: three
# handoffs of the same session, five daily logs with the same five bullet
# points. Relevance ranking alone serves all of them, and the page is spent
# before anything else is reached. MMR fixes exactly that: each pick is scored
# for relevance MINUS its similarity to what has already been picked.

MMR_DEFAULT_LAMBDA = 0.5
"""Relevance/diversity trade-off. 1.0 is a no-op (pure relevance); 0.0 ignores
relevance entirely.

0.5 is calibrated against measured profiles from this vault's own shape rather
than picked by feel: three variants of one session handoff score 0.91-0.95
against each other, while unrelated records score 0.09-0.19. Cubed (see
``REDUNDANCY_EXPONENT``) that becomes 0.76-0.86 versus 0.001-0.007 — so at 0.5
a genuine duplicate is pushed back by more than half the list, and a merely
same-topic neighbour moves by under a percent."""

REDUNDANCY_EXPONENT = 3
"""The redundancy penalty is ``similarity ** 3``, not ``similarity``.

Linear similarity cannot separate "duplicate" from "same topic": both sit well
above zero, so a linear penalty either leaves duplicates in place or punishes
every topically-related record with them. Cubing exploits the gap that is
actually there in the data — near-duplicates cluster above 0.9, everything else
below 0.2 — turning a 5x difference in similarity into a 100x difference in
penalty."""

MMR_TEXT_CHARS = 2000
"""Per-event character budget for the lexical token profile. Bounds the O(n*m)
tokenization on very large payloads; the opening 2000 characters of a handoff
or a daily log are more than enough to recognize a duplicate."""

MMR_MAX_CANDIDATES = 200
"""Hard ceiling on the candidate list. MMR is O(n^2) in the number of
candidates; beyond this the tail is passed through untouched rather than paid
for. Recall's own fetch window is far below this."""

_MMR_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def _collect_payload_strings(value: object, out: list[str], budget: list[int]) -> None:
    """Depth-first collect string leaves of a payload into ``out``.

    ``budget`` is a one-element list used as a mutable counter so the walk can
    stop as soon as ``MMR_TEXT_CHARS`` worth of text has been gathered. Payload
    shapes differ per content type (text / event / compound parts), so this
    stays shape-agnostic rather than knowing every variant.
    """
    if budget[0] <= 0:
        return
    if isinstance(value, str):
        out.append(value[: budget[0]])
        budget[0] -= len(value)
        return
    if isinstance(value, dict):
        for key in sorted(value):  # sorted → deterministic regardless of insertion order
            _collect_payload_strings(value[key], out, budget)
            if budget[0] <= 0:
                return
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _collect_payload_strings(item, out, budget)
            if budget[0] <= 0:
                return


def event_token_profile(event: Event) -> dict[str, float]:
    """L2-normalized token-frequency vector for one event's payload text.

    Word tokens only (digits and punctuation dropped): two daily logs differ in
    their dates but not in their substance, and it is the substance we want to
    recognize as duplicated. Returns an empty dict for an event with no text,
    which makes every similarity involving it zero — a payload we cannot read is
    never treated as a duplicate of anything.
    """
    parts: list[str] = []
    _collect_payload_strings(event.payload, parts, [MMR_TEXT_CHARS])
    counts: dict[str, float] = {}
    for token in _MMR_TOKEN_RE.findall(" ".join(parts).lower()):
        if len(token) < 3:  # articles, pronouns, glue — noise for duplicate detection
            continue
        counts[token] = counts.get(token, 0.0) + 1.0
    if not counts:
        return {}
    norm = math.sqrt(sum(v * v for v in counts.values()))
    return {k: v / norm for k, v in counts.items()}


def lexical_similarity(a: dict[str, float], b: dict[str, float]) -> float:
    """Cosine similarity of two normalized token profiles, in [0.0, 1.0]."""
    if not a or not b:
        return 0.0
    # Iterate the smaller profile — the intersection is the same either way.
    small, large = (a, b) if len(a) <= len(b) else (b, a)
    total = 0.0
    for token, weight in small.items():
        other = large.get(token)
        if other is not None:
            total += weight * other
    return max(0.0, min(1.0, total))


def mmr_rerank(
    events: list[Event],
    *,
    limit: int,
    lambda_: float = MMR_DEFAULT_LAMBDA,
    profile: Callable[[Event], dict[str, float]] | None = None,
) -> list[Event]:
    """Reorder an already-ranked list so near-duplicates sink.

    Greedy Maximal Marginal Relevance. At each step the next pick maximizes::

        lambda_ * relevance(d)  -  (1 - lambda_) * max_similarity(d, picked) ** 3

    The cubed similarity is the deliberate deviation from textbook MMR: see
    ``REDUNDANCY_EXPONENT`` for why a linear penalty cannot tell a duplicate
    apart from a same-topic neighbour on this kind of data.

    ``relevance`` is derived from the INCOMING order (position 0 scores 1.0,
    the last position scores ~0.0), so this is a pure reordering of whatever
    ranking the caller already produced — it never re-scores against the query
    and never introduces or drops a candidate.

    Fully deterministic: ties resolve to the earlier incoming position, so an
    unchanged input always yields an unchanged output. ``lambda_=1.0`` is
    exactly the identity (truncated to ``limit``).

    ``profile`` is injectable so a future caller can supply embedding-based
    vectors instead of the lexical default without touching this function.
    """
    if limit <= 0:
        return []
    if len(events) <= 1 or lambda_ >= 1.0:
        return events[:limit]

    head = events[:MMR_MAX_CANDIDATES]
    tail = events[MMR_MAX_CANDIDATES:]

    make_profile = profile or event_token_profile
    profiles = [make_profile(e) for e in head]
    n = len(head)
    # Linear relevance from rank position: first = 1.0, last = 0.0. Rank is the
    # only relevance signal that survives an RRF merge, so we use it directly
    # instead of inventing a score the merge never produced.
    relevance = [1.0 - (i / (n - 1)) for i in range(n)] if n > 1 else [1.0]

    selected: list[int] = []
    remaining = list(range(n))
    # Running max similarity of each remaining candidate against the selected
    # set — updated incrementally so the loop stays O(n^2), not O(n^3).
    max_sim = [0.0] * n

    while remaining and len(selected) < limit:
        best_idx = remaining[0]
        best_score = -math.inf
        for idx in remaining:
            penalty = max_sim[idx] ** REDUNDANCY_EXPONENT
            score = lambda_ * relevance[idx] - (1.0 - lambda_) * penalty
            # Strict > keeps the earlier (better-ranked) candidate on a tie.
            if score > best_score:
                best_score = score
                best_idx = idx
        selected.append(best_idx)
        remaining.remove(best_idx)
        chosen_profile = profiles[best_idx]
        for idx in remaining:
            sim = lexical_similarity(profiles[idx], chosen_profile)
            if sim > max_sim[idx]:
                max_sim[idx] = sim

    out = [head[i] for i in selected]
    if len(out) < limit:
        out.extend(tail[: limit - len(out)])
    return out
