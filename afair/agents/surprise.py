"""Per-event surprise signal for mode-switching.

The recall path already computes a *per-hit* surprise score (entity
novelty of a recall hit against the recent context window, in
``handlers._compute_surprise_score``). That score is surfaced on each
recall hit so an AI client can hedge on novel material.

This module computes the *per-event* sibling: walking the recent
event window in time order and asking, for each event as it arrived,
how much of its entity set was new relative to everything seen earlier
in the window. Summed across the window it becomes a cumulative
surprise reading the :class:`ModeSwitcher` folds into its CEN/DMN
decision, so a burst of genuinely novel material can shift attention
to focused mode even before the salience worker has caught up.

Pure substrate-derived, no LLM call. It reuses the entity graph the
:class:`EntityCanonicalizer` already materialized (``entity_mentions``
resolved through merges to canonical ids), so it scales linearly with
the window size, not the vault size.

Design notes
============
* Novelty is measured *within the window* against a running set of
  entities seen in strictly-earlier events. The first event in the
  window therefore always reads as fully novel (nothing precedes it).
  That is a constant offset, not a bug: the signal the mode-switcher
  cares about is the *relative burst* of new material, and the
  thresholds are calibrated with the offset baked in.
* Events with no canonical entities (not yet canonicalized, or simply
  entity-free) contribute 0.0 and do not advance the running set,
  exactly as the recall-side score returns ``None`` for entity-less
  hits.
* Window covers ``remember`` + ``observe`` (the user-driven kinds),
  matching ``_recent_canonical_context`` and the salience window.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..substrate.entities import resolve_canonical_batch

if TYPE_CHECKING:
    import sqlite3

# Kinds that count toward the surprise window. Mirrors the salience
# window and the recall-side recent-context window.
SURPRISE_KIND_FILTER = ("remember", "observe")


def read_recent_surprise(
    conn: sqlite3.Connection, *, limit: int = 100
) -> list[tuple[str, float, str]]:
    """Return ``[(event_id, surprise, created_at)]`` for the last
    ``limit`` user-driven events, most-recent-first (symmetric with
    :func:`afair.agents.salience.read_recent_salience`).

    ``surprise`` is the fraction of the event's canonical entities that
    were novel relative to events earlier in the window: 0.0 = every
    entity was already on the user's mind, 1.0 = all new. Entity-less
    events score 0.0.

    One indexed query for the window, one for the mentions, one batched
    canonical resolve, then a single in-Python pass.
    """
    if limit <= 0:
        return []

    window = conn.execute(
        """
        SELECT id, created_at FROM events
        WHERE kind IN ('remember', 'observe')
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    if not window:
        return []

    # Chronological (oldest-first) so the running-familiarity set grows
    # the way the events actually arrived.
    chronological = list(reversed(window))
    event_ids = [r["id"] for r in chronological]

    # Positional ? placeholders, all ids bound as params — no interpolation
    # of user data into SQL.
    placeholders = ",".join("?" * len(event_ids))
    mention_rows = conn.execute(
        f"""
        SELECT event_id, entity_id FROM entity_mentions
        WHERE event_id IN ({placeholders})
        """,
        event_ids,
    ).fetchall()

    raw_ids = list({m["entity_id"] for m in mention_rows})
    resolved = resolve_canonical_batch(conn, raw_ids) if raw_ids else {}

    by_event: dict[str, set[str]] = {}
    for m in mention_rows:
        canonical = resolved.get(m["entity_id"], m["entity_id"])
        by_event.setdefault(m["event_id"], set()).add(canonical)

    seen: set[str] = set()
    chrono_out: list[tuple[str, float, str]] = []
    for row in chronological:
        entities = by_event.get(row["id"], set())
        if not entities:
            novelty = 0.0
        else:
            novel = entities - seen
            novelty = len(novel) / len(entities)
            seen |= entities
        chrono_out.append((row["id"], novelty, row["created_at"]))

    # Most-recent-first for symmetry with read_recent_salience.
    return list(reversed(chrono_out))


def cumulative_surprise(conn: sqlite3.Connection, *, limit: int = 100) -> float:
    """Sum the per-event surprise over the recent window.

    Convenience wrapper the :class:`ModeSwitcher` calls; ranges
    ``[0, n]`` where ``n`` is the number of entity-bearing events in
    the window (each contributes at most 1.0).
    """
    return sum(score for _, score, _ in read_recent_surprise(conn, limit=limit))
