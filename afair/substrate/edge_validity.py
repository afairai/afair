"""Append-only bi-temporal validity for entity facts (ADR-0008).

Graphiti's useful distinction is preserved without importing its graph store:
``valid_from`` / ``valid_to`` describe when a fact was true in the world,
while ``recorded_at`` / an edge invalidation describe when afair knew the
corresponding version.  The immutable validity columns on ``entity_edges``
remain the at-discovery snapshot; later corrections are new rows in
``edge_validity_spans`` and the latest row composes the effective view.

No UPDATE or DELETE is used.  A bumped ``recorded_by`` version re-derives a
span while the earlier interpretation remains queryable (I2/I3/I7).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from ulid import ULID

from .sqlutil import iter_param_chunks

REFERENCE_TIME_VERSION = "edge-validity:reference-time:v1"
"""Low-confidence fallback when no temporal interpretation exists yet."""


class EdgeValiditySpan(BaseModel):
    id: str
    edge_id: str
    valid_from: str | None
    valid_to: str | None
    recorded_at: str
    recorded_by: str
    source_event_id: str | None
    confidence: float
    reason: str


class EffectiveEdgeValidity(BaseModel):
    """The composed two-clock view for one edge."""

    edge_id: str
    valid_from: str | None
    valid_to: str | None
    discovered_at: str
    expired_at: str | None
    confidence: float
    recorded_at: str
    recorded_by: str
    source_event_id: str | None
    reason: str


def normalize_temporal_bound(value: str | datetime | None) -> str | None:
    """Return one comparable UTC ISO-8601 timestamp.

    Date-only values become midnight UTC.  Naive datetimes are interpreted as
    UTC because afair's substrate timestamps are UTC.  Rejecting malformed
    values at the write boundary prevents lexicographically incomparable dates
    from entering the graph.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            msg = f"invalid ISO-8601 temporal bound: {value!r}"
            raise ValueError(msg) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def write_edge_validity_span(
    conn: sqlite3.Connection,
    *,
    edge_id: str,
    recorded_by: str,
    confidence: float,
    reason: str,
    valid_from: str | datetime | None = None,
    valid_to: str | datetime | None = None,
    source_event_id: str | None = None,
) -> EdgeValiditySpan | None:
    """Append one validity interpretation, or return ``None`` on replay.

    Idempotency is enforced by the schema's expression index across edge,
    normalized interval, worker version, and source event.  To correct the same
    interval, bump ``recorded_by``; the previous row remains audit history.
    """
    start = normalize_temporal_bound(valid_from)
    end = normalize_temporal_bound(valid_to)
    if start is None and end is None:
        msg = "a validity span needs valid_from or valid_to"
        raise ValueError(msg)
    if start is not None and end is not None and end < start:
        msg = "valid_to must not precede valid_from"
        raise ValueError(msg)
    if not recorded_by.strip():
        msg = "recorded_by must be non-empty"
        raise ValueError(msg)
    if not reason.strip():
        msg = "reason must be non-empty"
        raise ValueError(msg)
    if not 0.0 <= confidence <= 1.0:
        msg = "confidence must be between 0 and 1"
        raise ValueError(msg)

    row_id = str(ULID())
    recorded_at = datetime.now(UTC).isoformat()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO edge_validity_spans (
                    id, edge_id, valid_from, valid_to, recorded_at,
                    recorded_by, source_event_id, confidence, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row_id,
                    edge_id,
                    start,
                    end,
                    recorded_at,
                    recorded_by.strip(),
                    source_event_id,
                    confidence,
                    reason.strip(),
                ),
            )
    except sqlite3.IntegrityError as exc:
        if "UNIQUE constraint" in str(exc):
            return None
        raise
    return EdgeValiditySpan(
        id=row_id,
        edge_id=edge_id,
        valid_from=start,
        valid_to=end,
        recorded_at=recorded_at,
        recorded_by=recorded_by.strip(),
        source_event_id=source_event_id,
        confidence=confidence,
        reason=reason.strip(),
    )


def latest_edge_validity(conn: sqlite3.Connection, edge_id: str) -> EdgeValiditySpan | None:
    row = conn.execute(
        """
        SELECT * FROM edge_validity_spans
        WHERE edge_id = ?
        ORDER BY recorded_at DESC, id DESC
        LIMIT 1
        """,
        (edge_id,),
    ).fetchone()
    return None if row is None else _row_to_span(row)


def latest_edge_validity_batch(
    conn: sqlite3.Connection, edge_ids: list[str]
) -> dict[str, EdgeValiditySpan]:
    """Latest sidecar row per edge in one bounded query."""
    if not edge_ids:
        return {}
    out: dict[str, EdgeValiditySpan] = {}
    for chunk in iter_param_chunks(list(dict.fromkeys(edge_ids))):
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT s.* FROM edge_validity_spans s
            WHERE s.edge_id IN ({placeholders})
              AND NOT EXISTS (
                  SELECT 1 FROM edge_validity_spans newer
                  WHERE newer.edge_id = s.edge_id
                    AND (newer.recorded_at > s.recorded_at
                         OR (newer.recorded_at = s.recorded_at AND newer.id > s.id))
              )
            """,
            chunk,
        ).fetchall()
        out.update({row["edge_id"]: _row_to_span(row) for row in rows})
    return out


def effective_edge_validity(conn: sqlite3.Connection, edge_id: str) -> EffectiveEdgeValidity | None:
    """Compose immutable discovery data, latest validity, and expiry clock."""
    edge = conn.execute("SELECT * FROM entity_edges WHERE id = ?", (edge_id,)).fetchone()
    if edge is None:
        return None
    span = latest_edge_validity(conn, edge_id)
    invalidation = conn.execute(
        """
        SELECT invalidated_at FROM edge_invalidations
        WHERE edge_id = ?
        ORDER BY invalidated_at ASC, id ASC
        LIMIT 1
        """,
        (edge_id,),
    ).fetchone()
    expired_at = invalidation["invalidated_at"] if invalidation is not None else None
    if span is None:
        return EffectiveEdgeValidity(
            edge_id=edge_id,
            valid_from=_safe_existing_bound(edge["valid_from"]),
            valid_to=_safe_existing_bound(edge["valid_to"]),
            discovered_at=edge["discovered_at"],
            expired_at=expired_at,
            confidence=float(edge["confidence"]),
            recorded_at=edge["discovered_at"],
            recorded_by=edge["discovered_by"],
            source_event_id=edge["source_event_id"],
            reason="immutable edge discovery snapshot",
        )
    return EffectiveEdgeValidity(
        edge_id=edge_id,
        valid_from=span.valid_from,
        valid_to=span.valid_to,
        discovered_at=edge["discovered_at"],
        expired_at=expired_at,
        confidence=span.confidence,
        recorded_at=span.recorded_at,
        recorded_by=span.recorded_by,
        source_event_id=span.source_event_id,
        reason=span.reason,
    )


def edge_is_valid_at(conn: sqlite3.Connection, edge_id: str, at: str | datetime) -> bool:
    """Whether the fact's world-time interval contains ``at``.

    Bounds use ``[valid_from, valid_to)``.  Unknown bounds are open.  This does
    not ask when afair learned the fact; use :func:`edge_was_known_at` for the
    independent transaction-time axis.
    """
    view = effective_edge_validity(conn, edge_id)
    if view is None:
        return False
    moment = normalize_temporal_bound(at)
    assert moment is not None
    if view.valid_from is not None and moment < view.valid_from:
        return False
    return view.valid_to is None or moment < view.valid_to


def edge_was_known_at(conn: sqlite3.Connection, edge_id: str, at: str | datetime) -> bool:
    """Whether afair had learned and not yet expired the edge at ``at``."""
    view = effective_edge_validity(conn, edge_id)
    if view is None:
        return False
    moment = normalize_temporal_bound(at)
    assert moment is not None
    discovered = normalize_temporal_bound(view.discovered_at)
    expired = normalize_temporal_bound(view.expired_at)
    assert discovered is not None
    if moment < discovered:
        return False
    return expired is None or moment < expired


def edge_visible_as_of(conn: sqlite3.Connection, edge_id: str, at: str | datetime) -> bool:
    """Combined historical lens: true then *and* known by afair then."""
    return edge_is_valid_at(conn, edge_id, at) and edge_was_known_at(conn, edge_id, at)


def write_initial_edge_validity(
    conn: sqlite3.Connection,
    *,
    edge_id: str,
    source_event_id: str,
    discovered_by: str,
    explicit_valid_from: str | datetime | None = None,
    explicit_valid_to: str | datetime | None = None,
) -> EdgeValiditySpan | None:
    """Seed validity regardless of canonicalizer/temporal worker ordering.

    Explicit edge bounds win.  Otherwise the newest ``event_temporal`` row is
    used.  If that worker has not run yet, the event's recorded time is the
    conservative Graphiti-style reference-time fallback at low confidence; a
    later worker run appends a better interpretation.
    """
    if explicit_valid_from is not None or explicit_valid_to is not None:
        return write_edge_validity_span(
            conn,
            edge_id=edge_id,
            valid_from=explicit_valid_from,
            valid_to=explicit_valid_to,
            recorded_by=f"{discovered_by}:explicit-validity",
            source_event_id=source_event_id,
            confidence=1.0,
            reason="explicit validity supplied with entity edge",
        )

    row = conn.execute(
        """
        SELECT e.created_at,
               t.temporal_class, t.event_time, t.relevance_horizon,
               t.confidence, t.computed_by
        FROM events e
        LEFT JOIN event_temporal t ON t.id = (
            SELECT et.id FROM event_temporal et
            WHERE et.event_id = e.id
            ORDER BY et.created_at DESC, et.id DESC
            LIMIT 1
        )
        WHERE e.id = ?
        """,
        (source_event_id,),
    ).fetchone()
    if row is None:
        return None
    if row["event_time"] is not None:
        start, end = _safe_derived_interval(
            row["event_time"], row["relevance_horizon"], row["temporal_class"]
        )
        if start is not None:
            return write_edge_validity_span(
                conn,
                edge_id=edge_id,
                valid_from=start,
                valid_to=end,
                recorded_by=f"edge-validity:{row['computed_by']}",
                source_event_id=source_event_id,
                confidence=float(row["confidence"]),
                reason=f"derived from event_temporal class={row['temporal_class']}",
            )
    return write_edge_validity_span(
        conn,
        edge_id=edge_id,
        valid_from=row["created_at"],
        recorded_by=REFERENCE_TIME_VERSION,
        source_event_id=source_event_id,
        confidence=0.35,
        reason="fallback to source event reference time; awaiting temporal interpretation",
    )


def sync_event_edge_validity(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    temporal_class: str,
    event_time: str | datetime | None,
    relevance_horizon: str | datetime | None,
    confidence: float,
    computed_by: str,
) -> int:
    """Append refined spans for every edge sourced from a classified event."""
    if event_time is None:
        return 0
    start, end = _safe_derived_interval(event_time, relevance_horizon, temporal_class)
    if start is None:
        return 0
    rows = conn.execute(
        "SELECT id FROM entity_edges WHERE source_event_id = ? ORDER BY id",
        (event_id,),
    ).fetchall()
    written = 0
    for row in rows:
        span = write_edge_validity_span(
            conn,
            edge_id=row["id"],
            valid_from=start,
            valid_to=end,
            recorded_by=f"edge-validity:{computed_by}",
            source_event_id=event_id,
            confidence=confidence,
            reason=f"derived from event_temporal class={temporal_class}",
        )
        if span is not None:
            written += 1
    return written


def backfill_missing_edge_validity(
    conn: sqlite3.Connection, *, limit: int = 1_000
) -> dict[str, int]:
    """Seed spans for legacy edges in deterministic, bounded batches.

    This is intentionally not run during ``open_db``: opening a vault remains
    schema-only and side-effect free beyond additive DDL.  The one-shot CLI can
    call this repeatedly until ``remaining`` reaches zero.  Re-runs are safe.
    """
    if limit < 1:
        msg = "limit must be at least 1"
        raise ValueError(msg)
    rows = conn.execute(
        """
        SELECT e.id, e.source_event_id, e.discovered_by, e.valid_from, e.valid_to
        FROM entity_edges e
        WHERE NOT EXISTS (
            SELECT 1 FROM edge_validity_spans s WHERE s.edge_id = e.id
        )
        ORDER BY e.discovered_at ASC, e.id ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    written = 0
    for row in rows:
        span = write_initial_edge_validity(
            conn,
            edge_id=row["id"],
            source_event_id=row["source_event_id"],
            discovered_by=row["discovered_by"],
            explicit_valid_from=row["valid_from"],
            explicit_valid_to=row["valid_to"],
        )
        if span is not None:
            written += 1
    remaining = conn.execute(
        """
        SELECT COUNT(*) FROM entity_edges e
        WHERE NOT EXISTS (
            SELECT 1 FROM edge_validity_spans s WHERE s.edge_id = e.id
        )
        """
    ).fetchone()[0]
    return {"examined": len(rows), "written": written, "remaining": int(remaining)}


def _safe_derived_interval(
    event_time: str | datetime,
    relevance_horizon: str | datetime | None,
    temporal_class: str,
) -> tuple[str | None, str | None]:
    """Normalize fallible LLM-derived dates without breaking the pipeline."""
    try:
        start = normalize_temporal_bound(event_time)
    except ValueError:
        return None, None
    end: str | None = None
    if temporal_class == "one_off" and relevance_horizon is not None:
        try:
            candidate = normalize_temporal_bound(relevance_horizon)
        except ValueError:
            candidate = None
        if candidate is not None and start is not None and candidate >= start:
            end = candidate
    return start, end


def _safe_existing_bound(value: str | None) -> str | None:
    """Read legacy validity defensively; malformed old data means unknown."""
    try:
        return normalize_temporal_bound(value)
    except ValueError:
        return None


def _row_to_span(row: Any) -> EdgeValiditySpan:
    return EdgeValiditySpan(
        id=row["id"],
        edge_id=row["edge_id"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        recorded_at=row["recorded_at"],
        recorded_by=row["recorded_by"],
        source_event_id=row["source_event_id"],
        confidence=float(row["confidence"]),
        reason=row["reason"],
    )
