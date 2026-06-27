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


def read_event_temporal_batch(
    conn: sqlite3.Connection, event_hashes: list[str]
) -> dict[str, EventTemporal]:
    """Latest temporal record per event hash, in one query (avoids N+1).

    ASC order means the newest row overwrites earlier ones in the dict, so each
    hash maps to its most recent record — matching ``read_event_temporal``.
    """
    if not event_hashes:
        return {}
    placeholders = ",".join("?" for _ in event_hashes)
    rows = conn.execute(
        f"SELECT * FROM event_temporal WHERE event_hash IN ({placeholders}) "
        "ORDER BY created_at ASC",
        list(event_hashes),
    ).fetchall()
    out: dict[str, EventTemporal] = {}
    for row in rows:
        out[row["event_hash"]] = _row_to_event_temporal(row)
    return out


# ── relevance scoring (Phase 2) ─────────────────────────────────────────────
#
# A [0,1] multiplier on a hit's recall rank. Conservative by construction:
# nothing is ever excluded (history stays findable, I3); the effect is scaled
# by the worker's confidence so a shaky inference barely moves ranking; and
# only the clock-driven + superseded classes decay in P2 — the rest return 1.0
# until later phases. See analysis/2026-06-27-memory-relevance-decay-spec.md.

_DECAY_FLOOR = 0.15
"""A decayed one-off never sinks below this — it drops in ranking but stays
retrievable. Decay re-orders; it does not delete (I2)."""

_SUPERSEDED_FLOOR = 0.15
HALF_LIFE_DAYS = 14.0
"""After an event's relevance horizon passes, its factor halves every two
weeks. Tunable; the self-improvement loop can own this later."""

# Classes that decay against a clock in P2. recurring/periodic re-surfacing and
# decaying/transient topic-warmth come in later phases.
_CLOCK_DECAY_CLASSES = frozenset({"one_off", "periodic"})


def temporal_relevance(record: EventTemporal, now: datetime) -> float:
    """How relevant a memory is *right now*, in [0,1], for recall ranking.

    Blends the raw class-based factor toward 1.0 by ``(1 - confidence)`` so a
    low-confidence inference barely moves ranking (spec: under-decay when
    unsure). Never returns 0 — decay only re-orders, never excludes.
    """
    raw = _raw_temporal_factor(record, now)
    conf = max(0.0, min(1.0, record.confidence))
    return raw * conf + 1.0 * (1.0 - conf)


def _raw_temporal_factor(record: EventTemporal, now: datetime) -> float:
    if record.temporal_class == "superseded" or record.closure_state == "superseded":
        return _SUPERSEDED_FLOOR
    if record.temporal_class in _CLOCK_DECAY_CLASSES:
        horizon = _parse_iso(record.relevance_horizon) or _parse_iso(record.event_time)
        if horizon is None or now <= horizon:
            return 1.0  # no usable date, or the moment hasn't passed yet
        days_past = (now - horizon).total_seconds() / 86400.0
        decay = float(0.5 ** (days_past / HALF_LIFE_DAYS))
        return max(_DECAY_FLOOR, decay)
    return 1.0


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 date or datetime, defaulting naive values to UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt
