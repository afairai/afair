"""DB surface for the edge-confidence overlay (ADR-0004).

``entity_edges.confidence`` is the immutable at-discovery snapshot. The CURRENT
belief strength of an edge is the latest row in ``edge_confidence_scores``,
falling back to the column when no score row exists. This module owns the
table-level writes and the single accessor every consumer routes through, so
the "two sources of truth" (column vs latest score) is mediated in exactly one
place.

Append-only per I2 (DB triggers enforce it): re-scoring writes a NEW row, never
mutates. The pure model lives in ``confidence.py``; this module only persists
and reads what that model computes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from ulid import ULID

from .sqlutil import iter_param_chunks

if TYPE_CHECKING:
    import sqlite3


EDGE_CONFIDENCE_EPSILON = 0.01
"""Minimum change from the latest score before the scorer appends a new row —
re-runs with unchanged signals are no-ops, real signal changes produce
history."""


class EdgeConfidenceScore(BaseModel):
    """One append-only confidence score for an edge, with its full explanation."""

    id: str
    edge_id: str
    confidence: float
    components: dict[str, Any]
    computed_by: str
    computed_at: str


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def write_edge_confidence_score(
    conn: sqlite3.Connection,
    *,
    edge_id: str,
    confidence: float,
    components: dict[str, Any],
    computed_by: str,
) -> EdgeConfidenceScore:
    """Append one confidence score for an edge.

    Validates the edge exists with a clean :class:`ValueError` (like
    ``record_edge_review`` does) rather than surfacing a raw FK IntegrityError
    from deep in the insert — callers may pass ids recovered from recall hits.
    """
    if conn.execute("SELECT 1 FROM entity_edges WHERE id = ?", (edge_id,)).fetchone() is None:
        msg = f"entity_edge not found: {edge_id!r}"
        raise ValueError(msg)
    row_id = str(ULID())
    computed_at = _now_iso()
    with conn:
        conn.execute(
            """
            INSERT INTO edge_confidence_scores (
                id, edge_id, confidence, components, computed_by, computed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                edge_id,
                confidence,
                json.dumps(components, ensure_ascii=False, sort_keys=True),
                computed_by,
                computed_at,
            ),
        )
    return EdgeConfidenceScore(
        id=row_id,
        edge_id=edge_id,
        confidence=confidence,
        components=components,
        computed_by=computed_by,
        computed_at=computed_at,
    )


def latest_edge_confidence_batch(conn: sqlite3.Connection, edge_ids: list[str]) -> dict[str, float]:
    """The latest served confidence per edge for a batch, in one query.

    Ascending-order-overwrite trick (same as ``latest_edge_reviews_batch``):
    the dict ends up holding each edge's most recent score regardless of
    ``computed_by`` version. Edges with no score row are ABSENT from the
    result — callers fall back to ``entity_edges.confidence``.
    """
    if not edge_ids:
        return {}
    out: dict[str, float] = {}
    for chunk in iter_param_chunks(edge_ids):
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT edge_id, confidence FROM edge_confidence_scores "
            f"WHERE edge_id IN ({placeholders}) "
            "ORDER BY computed_at ASC, id ASC",
            chunk,
        ).fetchall()
        # Ascending order → later row overwrites earlier, so the latest score
        # per edge wins. Each id is in exactly one chunk, so the cross-chunk
        # merge can't conflict.
        for row in rows:
            out[row["edge_id"]] = float(row["confidence"])
    return out


def latest_edge_scores_batch(
    conn: sqlite3.Connection, edge_ids: list[str]
) -> dict[str, EdgeConfidenceScore]:
    """Full latest score row per edge — used by the scorer's epsilon check and
    by tests that need the components blob. Same latest-wins semantics as
    :func:`latest_edge_confidence_batch`."""
    if not edge_ids:
        return {}
    out: dict[str, EdgeConfidenceScore] = {}
    for chunk in iter_param_chunks(edge_ids):
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT * FROM edge_confidence_scores "
            f"WHERE edge_id IN ({placeholders}) "
            "ORDER BY computed_at ASC, id ASC",
            chunk,
        ).fetchall()
        for row in rows:
            try:
                components = json.loads(row["components"])
            except (ValueError, TypeError):
                components = {}
            out[row["edge_id"]] = EdgeConfidenceScore(
                id=row["id"],
                edge_id=row["edge_id"],
                confidence=float(row["confidence"]),
                components=components,
                computed_by=row["computed_by"],
                computed_at=row["computed_at"],
            )
    return out
