"""Temporal-relevance substrate primitives (relevance-decay design, Phase 1).

Owns the table-level surface for ``event_temporal`` — the derived, append-only
time/relevance metadata the temporal worker infers per event. The inference
*logic* lives in ``agents/temporal.py``; this module only persists it.

Append-only (I2): no UPDATE, no DELETE (enforced by schema triggers).
Re-derivable (I3/I7): bumping the worker's ``computed_by`` version writes new
rows over the unchanged substrate. Decay is a recall score, never a mutation.

See ``analysis/2026-06-27-memory-relevance-decay-spec.md`` for the full design
(the eight relevance classes and how recall will later consume them).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel
from ulid import ULID

if TYPE_CHECKING:
    import sqlite3


# The inferred temporal classes (spec §2). Inferred from content by the worker,
# never an ontology the user fills in (I6); this tuple is just the worker's
# vocabulary, used to validate an LLM verdict before it is stored.
TEMPORAL_CLASSES: tuple[str, ...] = (
    "one_off",  # a dated single event: appointment, deadline, flight
    "recurring",  # birthday, anniversary, weekly standup
    "superseded",  # a fact a later event invalidated
    "decaying",  # a topic whose relevance fades as it goes quiet
    "transient",  # ephemeral, low durable value
    "evergreen",  # timeless: a name, a stable preference, who someone is
    "periodic",  # seasonal / long-cycle: tax season, passport renewal
    "commitment",  # a promise, salient until fulfilled
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_row_id() -> str:
    return str(ULID())


class EventTemporal(BaseModel):
    """One row of event_temporal — derived, append-only time metadata."""

    id: str
    event_id: str
    event_hash: str
    temporal_class: str
    event_time: str | None
    relevance_horizon: str | None
    recurrence_rule: str | None
    closure_state: str | None
    confidence: float
    computed_by: str
    created_at: str


def write_event_temporal(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    event_hash: str,
    temporal_class: str,
    confidence: float,
    computed_by: str,
    event_time: str | None = None,
    relevance_horizon: str | None = None,
    recurrence_rule: str | None = None,
    closure_state: str | None = None,
) -> EventTemporal | None:
    """Persist one temporal record.

    Idempotent on ``(event_hash, computed_by)``: re-running the same worker
    version on the same event is a no-op that returns ``None``. A bumped
    ``computed_by`` writes a fresh row (re-derivation, I7).
    """
    row_id = _new_row_id()
    created_at = _now_iso()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO event_temporal (
                    id, event_id, event_hash, temporal_class, event_time,
                    relevance_horizon, recurrence_rule, closure_state,
                    confidence, computed_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row_id,
                    event_id,
                    event_hash,
                    temporal_class,
                    event_time,
                    relevance_horizon,
                    recurrence_rule,
                    closure_state,
                    confidence,
                    computed_by,
                    created_at,
                ),
            )
    except Exception as exc:  # narrowed on the message below
        if "UNIQUE constraint" in str(exc):
            return None
        raise
    return EventTemporal(
        id=row_id,
        event_id=event_id,
        event_hash=event_hash,
        temporal_class=temporal_class,
        event_time=event_time,
        relevance_horizon=relevance_horizon,
        recurrence_rule=recurrence_rule,
        closure_state=closure_state,
        confidence=confidence,
        computed_by=computed_by,
        created_at=created_at,
    )


def read_event_temporal(conn: sqlite3.Connection, event_hash: str) -> EventTemporal | None:
    """The most recent temporal record for an event, or ``None``."""
    row = conn.execute(
        """
        SELECT * FROM event_temporal
        WHERE event_hash = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (event_hash,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_event_temporal(row)


def _row_to_event_temporal(row: sqlite3.Row) -> EventTemporal:
    return EventTemporal(
        id=row["id"],
        event_id=row["event_id"],
        event_hash=row["event_hash"],
        temporal_class=row["temporal_class"],
        event_time=row["event_time"],
        relevance_horizon=row["relevance_horizon"],
        recurrence_rule=row["recurrence_rule"],
        closure_state=row["closure_state"],
        confidence=float(row["confidence"]),
        computed_by=row["computed_by"],
        created_at=row["created_at"],
    )
