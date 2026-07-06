"""Temporal-relevance substrate primitives (relevance-decay design, Phase 1).

Owns the table-level surface for ``event_temporal`` — the derived, append-only
time/relevance metadata the temporal worker infers per event. The inference
*logic* lives in ``agents/temporal.py``; this module only persists it.

Append-only (I2): no UPDATE, no DELETE (enforced by schema triggers).
Re-derivable (I3/I7): bumping the worker's ``computed_by`` version writes new
rows over the unchanged substrate. Decay is a recall score, never a mutation.

See the relevance-decay design notes for the full picture
(the eight relevance classes and how recall will later consume them).
"""

from __future__ import annotations

import calendar
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from pydantic import BaseModel
from ulid import ULID

if TYPE_CHECKING:
    from collections.abc import Callable


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
    except sqlite3.IntegrityError as exc:
        # A UNIQUE-constraint violation = this temporal row already exists;
        # treat as a no-op. Any other integrity error (NOT NULL / FK / CHECK)
        # is a real bug and propagates.
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
# until later phases.

_DECAY_FLOOR = 0.15
"""A decayed one-off never sinks below this — it drops in ranking but stays
retrievable. Decay re-orders; it does not delete (I2)."""

_SUPERSEDED_FLOOR = 0.15
HALF_LIFE_DAYS = 14.0
"""After an event's relevance horizon passes, its factor halves every two
weeks. Tunable; the self-improvement loop can own this later."""

# Classes that decay against a clock (P2). A periodic item WITH a usable
# recurrence rule re-surfaces instead (P3); without one it falls back to decay.
_CLOCK_DECAY_CLASSES = frozenset({"one_off", "periodic"})

# Re-surfacing (P3). A recurring/periodic item is fully relevant within this
# many days of its next occurrence and sits at a floor between occurrences. A
# commitment is fully relevant while open and recedes once fulfilled.
_RECUR_WINDOW_DAYS = 14.0
_RECUR_FLOOR = 0.5
_COMMITMENT_DONE_FLOOR = 0.3

# Topic warmth (P4). A transient memory fades fast; a decaying topic fades
# slowly as it goes quiet. Both decay against the memory's own age (the record's
# created_at, a close proxy for when the event landed). New activity on a topic
# is itself new, recent events — so a topic that stays alive keeps fresh,
# high-relevance memories while the stale ones sink. Tunable; a future hand-off
# to the self-improvement tuner can own these curves.
_TRANSIENT_HALF_LIFE_DAYS = 2.0
_DECAYING_HALF_LIFE_DAYS = 60.0


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
    cls = record.temporal_class
    if cls == "superseded" or record.closure_state == "superseded":
        return _SUPERSEDED_FLOOR
    if cls == "commitment":
        # Salient while open, recedes once fulfilled (P3).
        return _COMMITMENT_DONE_FLOOR if record.closure_state == "fulfilled" else 1.0
    if cls == "transient":
        # Ephemeral — fades fast against its own age (P4).
        return _age_decay(record, now, _TRANSIENT_HALF_LIFE_DAYS)
    if cls == "decaying":
        # A topic that goes quiet fades slowly; new activity is itself recent
        # events that stay fresh (P4).
        return _age_decay(record, now, _DECAYING_HALF_LIFE_DAYS)
    # Recurrence (P3): a recurring item, or a periodic one with a usable rule,
    # re-surfaces near its next occurrence and recedes between — it does not
    # decay to a floor like a one-off.
    nxt = next_occurrence(record, now)
    if nxt is not None:
        days_to_next = abs((nxt - now).total_seconds()) / 86400.0
        return 1.0 if days_to_next <= _RECUR_WINDOW_DAYS else _RECUR_FLOOR
    if cls in _CLOCK_DECAY_CLASSES:
        # one-off, or periodic with no usable recurrence → clock decay (P2).
        horizon = _parse_iso(record.relevance_horizon) or _parse_iso(record.event_time)
        if horizon is None or now <= horizon:
            return 1.0  # no usable date, or the moment hasn't passed yet
        days_past = (now - horizon).total_seconds() / 86400.0
        decay = float(0.5 ** (days_past / HALF_LIFE_DAYS))
        return max(_DECAY_FLOOR, decay)
    return 1.0


def _age_decay(record: EventTemporal, now: datetime, half_life_days: float) -> float:
    """Exponential decay against the memory's own age, floored. The age anchor
    is the record's ``created_at`` (a close proxy for when the event landed)."""
    anchor = _parse_iso(record.created_at)
    if anchor is None or now <= anchor:
        return 1.0
    days = (now - anchor).total_seconds() / 86400.0
    return max(_DECAY_FLOOR, float(0.5 ** (days / half_life_days)))


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


# ── recurrence + re-surfacing (Phase 3) ─────────────────────────────────────
#
# A deliberately small RRULE reader: the common FREQ shapes the spec scopes
# (YEARLY for birthdays/anniversaries, MONTHLY/WEEKLY/DAILY). Anything richer
# returns None and the item simply doesn't re-surface, rather than guessing.


def next_occurrence(record: EventTemporal, now: datetime) -> datetime | None:
    """The next occurrence at or after ``now`` for a recurring/periodic record,
    from its ``event_time`` anchor and a simple ``FREQ=`` rule. ``None`` when
    there is no usable anchor or rule."""
    anchor = _parse_iso(record.event_time)
    freq = _rrule_freq(record.recurrence_rule)
    if anchor is None or freq is None:
        return None
    if anchor >= now:
        return anchor
    if freq == "YEARLY":
        return _advance_calendar(anchor, now, _add_year)
    if freq == "MONTHLY":
        return _advance_calendar(anchor, now, _add_month)
    if freq == "WEEKLY":
        return _advance_fixed(anchor, now, timedelta(weeks=1))
    if freq == "DAILY":
        return _advance_fixed(anchor, now, timedelta(days=1))
    return None


def next_relevant_moment(record: EventTemporal, now: datetime) -> datetime | None:
    """The next moment this memory becomes relevant: its next recurrence, or a
    still-future one-off / commitment time. ``None`` if nothing upcoming."""
    nxt = next_occurrence(record, now)
    if nxt is not None:
        return nxt
    event_time = _parse_iso(record.event_time)
    if event_time is not None and event_time >= now:
        return event_time
    return None


def _rrule_freq(rule: str | None) -> str | None:
    if not rule:
        return None
    for part in rule.split(";"):
        token = part.strip().upper()
        if token.startswith("FREQ="):
            return token[len("FREQ=") :]
    return None


def _advance_fixed(anchor: datetime, now: datetime, step: timedelta) -> datetime:
    steps = int((now - anchor) / step) + 1
    return anchor + step * steps


def _advance_calendar(
    anchor: datetime, now: datetime, step: Callable[[datetime], datetime]
) -> datetime:
    candidate = anchor
    # Bounded: a few iterations even for an anchor years/months in the past.
    while candidate < now:
        candidate = step(candidate)
    return candidate


def _add_year(dt: datetime) -> datetime:
    try:
        return dt.replace(year=dt.year + 1)
    except ValueError:  # Feb 29 → clamp to Feb 28 in a non-leap year
        return dt.replace(year=dt.year + 1, day=28)


def _add_month(dt: datetime) -> datetime:
    month = dt.month + 1
    year = dt.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def upcoming_temporal(
    conn: sqlite3.Connection, now: datetime, *, within_days: float, limit: int
) -> list[EventTemporal]:
    """Records whose next relevant moment falls within ``within_days``, soonest
    first. Recurring/periodic items near their next occurrence, plus one-offs
    and open commitments with a near future time. Latest record per event wins.
    Feeds the session-start ``upcoming`` re-surfacing (P3)."""
    horizon = now + timedelta(days=within_days)
    rows = conn.execute(
        """
        SELECT * FROM event_temporal
        WHERE temporal_class IN ('recurring', 'periodic', 'one_off', 'commitment')
        ORDER BY created_at DESC
        """
    ).fetchall()
    seen: set[str] = set()
    scored: list[tuple[datetime, EventTemporal]] = []
    for row in rows:
        event_hash = row["event_hash"]
        if event_hash in seen:
            continue
        seen.add(event_hash)  # DESC order → first seen is the newest record
        record = _row_to_event_temporal(row)
        when = next_relevant_moment(record, now)
        if when is not None and now <= when <= horizon:
            scored.append((when, record))
    scored.sort(key=lambda item: item[0])
    return [record for _, record in scored[:limit]]
