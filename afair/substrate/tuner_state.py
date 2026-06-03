"""
Persistence layer for the self-improvement tuner.

The ``tuner_state`` table records every action the tuner takes:
promotes (a new value won validation), rollbacks (an automatic
revert), hypotheses (candidates the tuner is considering), and
observations (signals it noted along the way).

Append-only. The CHECK constraint + UPDATE/DELETE triggers in
schema.py enforce that at the DB level (Invariant I2 + I7).

Read pattern: the latest 'promote' or 'rollback' for a given
(worker, tunable) wins. Callers (the TunableRegistry, mostly) use
:func:`current_value` to look up an active value, falling back to
the static default declared in the registry spec when no rows exist.

Write pattern: the tuner module is the only writer. Each write
captures enough evidence (judge scores, replay size, rationale) to
explain the decision when the operator reads it back via recall.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

import structlog
from ulid import ULID

if TYPE_CHECKING:
    import sqlite3


log = structlog.get_logger(__name__)


TunerKind = Literal["promote", "rollback", "hypothesis", "observation"]


@dataclass(frozen=True)
class TunerEvent:
    id: str
    recorded_at: str
    kind: TunerKind
    worker: str
    tunable: str
    old_value: Any | None
    new_value: Any | None
    evidence: dict[str, Any] | None
    rationale: str | None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _to_json(value: Any | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, default=str)


def _from_json(s: str | None) -> Any | None:
    if s is None:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        log.warning("tuner_state.parse_failed", raw=s[:200])
        return None


def write(
    conn: sqlite3.Connection,
    *,
    kind: TunerKind,
    worker: str,
    tunable: str,
    old_value: Any | None = None,
    new_value: Any | None = None,
    evidence: dict[str, Any] | None = None,
    rationale: str | None = None,
) -> TunerEvent:
    """Append one tuner_state row.

    Returns the row that was written (with the freshly minted id).
    Raises sqlite3.IntegrityError if the kind value isn't on the
    whitelist (CHECK constraint enforces this at the DB level).
    """
    event = TunerEvent(
        id=str(ULID()),
        recorded_at=_now_iso(),
        kind=kind,
        worker=worker,
        tunable=tunable,
        old_value=old_value,
        new_value=new_value,
        evidence=evidence,
        rationale=rationale,
    )
    with conn:
        conn.execute(
            """
            INSERT INTO tuner_state (
                id, recorded_at, kind, worker, tunable,
                old_value_json, new_value_json, evidence_json, rationale
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                event.recorded_at,
                event.kind,
                event.worker,
                event.tunable,
                _to_json(event.old_value),
                _to_json(event.new_value),
                _to_json(event.evidence),
                event.rationale,
            ),
        )
    log.info(
        "tuner_state.write",
        id=event.id,
        kind=event.kind,
        worker=event.worker,
        tunable=event.tunable,
    )
    return event


def current_value(
    conn: sqlite3.Connection,
    *,
    worker: str,
    tunable: str,
) -> Any | None:
    """Return the active value for (worker, tunable), or None if none was ever set.

    The active value is the ``new_value`` of the most recent ``promote``
    OR ``rollback`` row. Hypothesis / observation rows don't affect the
    active value. Rollback rows ALSO carry a new_value (the value being
    restored) so they participate in the lookup.
    """
    row = conn.execute(
        """
        SELECT new_value_json
        FROM tuner_state
        WHERE worker = ? AND tunable = ? AND kind IN ('promote', 'rollback')
        ORDER BY recorded_at DESC
        LIMIT 1
        """,
        (worker, tunable),
    ).fetchone()
    if row is None:
        return None
    return _from_json(row["new_value_json"])


def history(
    conn: sqlite3.Connection,
    *,
    worker: str | None = None,
    tunable: str | None = None,
    limit: int = 100,
) -> list[TunerEvent]:
    """Read recent tuner_state rows, optionally filtered.

    Used by the admin surface and by the tuner itself when deciding
    whether a tunable is in a cooldown period after a rollback.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if worker is not None:
        clauses.append("worker = ?")
        params.append(worker)
    if tunable is not None:
        clauses.append("tunable = ?")
        params.append(tunable)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT id, recorded_at, kind, worker, tunable,
               old_value_json, new_value_json, evidence_json, rationale
        FROM tuner_state
        {where}
        ORDER BY recorded_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [
        TunerEvent(
            id=r["id"],
            recorded_at=r["recorded_at"],
            kind=r["kind"],
            worker=r["worker"],
            tunable=r["tunable"],
            old_value=_from_json(r["old_value_json"]),
            new_value=_from_json(r["new_value_json"]),
            evidence=_from_json(r["evidence_json"]),
            rationale=r["rationale"],
        )
        for r in rows
    ]


def last_rollback_at(
    conn: sqlite3.Connection,
    *,
    worker: str,
    tunable: str,
) -> str | None:
    """ISO timestamp of the most recent rollback for (worker, tunable), or None."""
    row = conn.execute(
        """
        SELECT recorded_at FROM tuner_state
        WHERE worker = ? AND tunable = ? AND kind = 'rollback'
        ORDER BY recorded_at DESC
        LIMIT 1
        """,
        (worker, tunable),
    ).fetchone()
    return row["recorded_at"] if row else None
