"""Bi-temporal invalidation — append-only fact-supersession.

The pattern stolen from Graphiti: every fact has an implicit ``t_valid``
(the event's ``created_at``) and an optional ``t_invalid`` recorded
later when a contradicting fact arrives. Time-travel queries are then
possible: "what was true at point T" = facts where t_valid ≤ T and
t_invalid is either null or > T.

Where we beat Graphiti: their bi-temporal lives in a graph database
(Neo4j/Kuzu). Ours lives IN the substrate — an invalidation is itself
just another append-only event with ``kind='invalidate'`` and
``parent_hashes=[<target_event>]``. Zero schema migration, zero new
storage layer, fully I2-conformant. The substrate stays the single
source of truth; the bi-temporal view is derived at read time.

Default behavior: recall hits surface ``invalidation`` info but do
NOT filter invalidated facts. The AI client decides — current-state
queries prefer ``invalidation is None``; historical queries don't.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import BaseModel

from ..substrate.events import Event, write_event

if TYPE_CHECKING:
    import sqlite3


INVALIDATE_KIND = "invalidate"
"""Kind value for invalidation events. Stable forever per I1 (additive)."""


class InvalidationInfo(BaseModel):
    """When and why a fact was superseded by a later contradiction.

    Surfaces inside ``RecallHit.invalidation`` and
    ``GetEventResult.invalidation`` so the AI client can decide whether
    to consider the fact current. The substrate keeps both the original
    fact and this record forever (I2); this is just the projection.
    """

    at: str
    """ISO 8601 timestamp when the invalidation was recorded (t_invalid)."""

    by_event_id: str
    """ULID of the invalidation event — fetch with ``get_event`` for the
    full reason + context that triggered the supersession."""

    reason: str | None = None
    """Free-text reason supplied by the caller, if any."""


def write_invalidation(
    conn: sqlite3.Connection,
    *,
    target_hash: str,
    reason: str | None,
    origin: str,
) -> Event:
    """Record that ``target_hash`` is no longer considered current.

    Writes a new substrate event with ``kind='invalidate'`` whose payload
    references the target hash. ``parent_hashes`` carries the target so
    the substrate's lineage view shows the supersession explicitly.

    Returns the new invalidation event. The target event is NOT touched
    — I2 forbids it. Subsequent ``read_invalidation`` calls return the
    most-recent invalidation for the target.
    """
    payload = {
        "content_type": INVALIDATE_KIND,
        "target_hash": target_hash,
        "reason": reason,
    }
    return write_event(
        conn,
        origin=origin,
        kind=INVALIDATE_KIND,
        payload=payload,
        parent_hashes=[target_hash],
    )


def read_invalidation(conn: sqlite3.Connection, content_hash: str) -> InvalidationInfo | None:
    """Return the most recent invalidation of ``content_hash``, or None.

    "Most recent" means newest ``created_at`` — invalidation can itself
    be invalidated (rare; treated as "re-validation"), but the read-side
    contract is simple: latest wins. AI clients rarely need the chain.
    """
    row = conn.execute(
        """
        SELECT id, created_at, payload FROM events
        WHERE kind = ?
          AND json_extract(payload, '$.target_hash') = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (INVALIDATE_KIND, content_hash),
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row["payload"])
    return InvalidationInfo(
        at=row["created_at"],
        by_event_id=row["id"],
        reason=payload.get("reason"),
    )


def read_invalidations_batch(
    conn: sqlite3.Connection, content_hashes: list[str]
) -> dict[str, InvalidationInfo]:
    """Bulk variant — one SQL query for many hashes at once.

    Used by recall to avoid an N+1 lookup when surfacing invalidation
    info on every hit. Returns a dict keyed by the target content_hash;
    hashes with no invalidation are absent from the result (caller
    should treat that as ``None``).
    """
    if not content_hashes:
        return {}
    placeholders = ",".join("?" * len(content_hashes))
    rows = conn.execute(
        f"""
        SELECT id, created_at, payload,
               json_extract(payload, '$.target_hash') AS target_hash
        FROM events
        WHERE kind = ?
          AND target_hash IN ({placeholders})
        ORDER BY target_hash, created_at DESC
        """,
        (INVALIDATE_KIND, *content_hashes),
    ).fetchall()
    result: dict[str, InvalidationInfo] = {}
    for row in rows:
        target = row["target_hash"]
        if target in result:
            continue  # already have newer (sorted DESC by created_at)
        payload = json.loads(row["payload"])
        result[target] = InvalidationInfo(
            at=row["created_at"],
            by_event_id=row["id"],
            reason=payload.get("reason"),
        )
    return result
