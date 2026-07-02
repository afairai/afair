"""EdgeConfidenceScorer cold-path worker (ADR-0004 S4).

The write-time prior (S3) is only as good as the signals available WHEN the
canonicalizer discovers an edge. This worker is the second layer: a bounded,
LLM-free pass that (a) backfills the 176 legacy flat-0.8 edges with a real
score computed over signals recovered from the substrate, and (b) re-scores
edges whose post-write signals have moved (a sibling triple landed →
corroboration up; the conflict resolver judged the source contested →
confidence down).

Storage discipline (ADR-0004): the immutable ``entity_edges.confidence`` column
is never touched (I2/I3 — the DB triggers refuse). Every score is an append-only
row in ``edge_confidence_scores`` stamped ``EDGE_CONFIDENCE_VERSION``. A re-run
with unchanged signals appends nothing (idempotent): a new row lands only when
no current-version row exists yet, or the recomputed value differs from the
latest by >= EDGE_CONFIDENCE_EPSILON. Re-derivation under a new model is a
version bump; old rows stay as history (I7).

No LLM, so no budget pressure — pure SQL + the pure model in
``substrate/confidence.py``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import structlog

from ..substrate import pipeline_events as pe
from ..substrate.confidence import (
    EDGE_CONFIDENCE_VERSION,
    EdgeConfidenceSignals,
    calibration_report,
    compute_edge_confidence,
)
from ..substrate.edge_confidence import (
    EDGE_CONFIDENCE_EPSILON,
    latest_edge_scores_batch,
    write_edge_confidence_score,
)
from ..substrate.entities import EntityEdge, count_corroborating_sources
from .cold_path import ColdPathWorker
from .conflict_resolver import read_conflicts_batch
from .verdicts import is_unresolved_conflict

if TYPE_CHECKING:
    import sqlite3

    from ..settings import Settings

log = structlog.get_logger(__name__)


MAX_EDGES_PER_CYCLE = 100
"""Hard cap on edges scored per cycle. With 176 legacy edges the backfill
completes in two cycles; steady-state re-scoring is far smaller."""


class EdgeConfidenceScorer(ColdPathWorker):
    """Cold-path worker that backfills + re-scores edge confidence (ADR-0004)."""

    name = "edge_confidence_scorer"
    interval_seconds = 240  # offset from canonicalizer (120) / temporal (180)

    def run(self, conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "edges_scored": 0,
            "edges_skipped_unchanged": 0,
            "legacy_backfilled": 0,
        }
        base_rate, corroboration_weight = _resolve_weights(conn)

        edges = _select_edges_to_score(conn, MAX_EDGES_PER_CYCLE)
        if edges:
            edge_ids = [e.id for e in edges]
            latest_v1 = _latest_v1_confidence(conn, edge_ids)
            latest_any = latest_edge_scores_batch(conn, edge_ids)
            for edge in edges:
                new_conf, components = compute_edge_confidence(
                    _recover_signals(conn, edge),
                    base_rate=base_rate,
                    corroboration_weight=corroboration_weight,
                )
                prev_v1 = latest_v1.get(edge.id)
                if prev_v1 is not None and abs(new_conf - prev_v1) < EDGE_CONFIDENCE_EPSILON:
                    stats["edges_skipped_unchanged"] += 1
                    continue
                write_edge_confidence_score(
                    conn,
                    edge_id=edge.id,
                    confidence=new_conf,
                    components=components,
                    computed_by=EDGE_CONFIDENCE_VERSION,
                )
                stats["edges_scored"] += 1
                if edge.id not in latest_any:
                    # No score row of ANY version existed → a genuine legacy
                    # flat-0.8 edge getting its first real score.
                    stats["legacy_backfilled"] += 1

        # Calibration: measure the priors against the operator's verdicts.
        # Included in cycle stats only once reviews exist (bootstrap).
        report = calibration_report(conn)
        if report["reviewed"] > 0:
            stats["calibration_reviewed"] = report["reviewed"]
            stats["calibration_sufficient"] = report["sufficient"]
            stats["calibration_brier"] = report["brier"]

        pe.record(
            conn,
            event_id="-",
            stage="edge_scorer.cycle",
            producer="edge_confidence_scorer:v0",
            detail=(
                f"scored={stats['edges_scored']} "
                f"skipped_unchanged={stats['edges_skipped_unchanged']} "
                f"legacy_backfilled={stats['legacy_backfilled']}"
            ),
        )
        return stats


# ── weight resolution (registry, S8) ───────────────────────────────────────


def _resolve_weights(conn: sqlite3.Connection) -> tuple[float, float]:
    """Resolve base_rate + corroboration_weight through the tuner registry,
    falling back to the module defaults (surprise-window pattern). Until S8
    registers the specs, ``registry.get`` raises KeyError and the except path
    serves the pure-model defaults — so this worker ships before S8."""
    from ..substrate.confidence import DEFAULT_BASE_RATE, W_CORROBORATION

    base_rate = DEFAULT_BASE_RATE
    corroboration_weight = W_CORROBORATION
    try:
        from .tunable_registry import TunableRegistry

        registry = TunableRegistry(conn)
        base_rate = float(registry.get("edge_confidence", "base_rate"))
        corroboration_weight = float(registry.get("edge_confidence", "corroboration_weight"))
    except Exception:
        return DEFAULT_BASE_RATE, W_CORROBORATION
    return base_rate, corroboration_weight


# ── selection ───────────────────────────────────────────────────────────────


def _select_edges_to_score(conn: sqlite3.Connection, limit: int) -> list[EntityEdge]:
    """Edges to (re)score this cycle, capped at ``limit``.

    Priority 1: edges with NO current-version score row, oldest ``discovered_at``
    first — the backfill of legacy + write-time-only edges. Priority 2 (fills
    the remaining budget): already-scored edges, most-recent first, re-evaluated
    so post-write signal changes (new corroboration, a contested source) land a
    fresh score. The epsilon check in ``run`` keeps unchanged re-evaluations
    from writing, so this is idempotent.
    """
    unscored = conn.execute(
        """
        SELECT * FROM entity_edges e
        WHERE NOT EXISTS (
            SELECT 1 FROM edge_confidence_scores s
            WHERE s.edge_id = e.id AND s.computed_by = ?
        )
        ORDER BY e.discovered_at ASC
        LIMIT ?
        """,
        (EDGE_CONFIDENCE_VERSION, limit),
    ).fetchall()
    edges = [_row_to_edge(r) for r in unscored]
    remaining = limit - len(edges)
    if remaining > 0:
        scored = conn.execute(
            """
            SELECT * FROM entity_edges e
            WHERE EXISTS (
                SELECT 1 FROM edge_confidence_scores s
                WHERE s.edge_id = e.id AND s.computed_by = ?
            )
            ORDER BY e.discovered_at DESC
            LIMIT ?
            """,
            (EDGE_CONFIDENCE_VERSION, remaining),
        ).fetchall()
        edges.extend(_row_to_edge(r) for r in scored)
    return edges


def _latest_v1_confidence(conn: sqlite3.Connection, edge_ids: list[str]) -> dict[str, float]:
    """Latest CURRENT-VERSION score per edge (for the epsilon check)."""
    if not edge_ids:
        return {}
    placeholders = ",".join("?" * len(edge_ids))
    rows = conn.execute(
        f"SELECT edge_id, confidence FROM edge_confidence_scores "
        f"WHERE computed_by = ? AND edge_id IN ({placeholders}) "
        "ORDER BY computed_at ASC, id ASC",
        (EDGE_CONFIDENCE_VERSION, *edge_ids),
    ).fetchall()
    return {r["edge_id"]: float(r["confidence"]) for r in rows}


# ── signal recovery ─────────────────────────────────────────────────────────


def _recover_signals(conn: sqlite3.Connection, edge: EntityEdge) -> EdgeConfidenceSignals:
    """Recover the edge-confidence signals from the substrate for a stored edge.

    Every signal degrades to None/0 gracefully (I3): a legacy edge whose
    extractor interpretation is unrecoverable still gets a sensible score from
    crispness + corroboration alone.
    """
    event_hash = _source_event_hash(conn, edge.source_event_id)
    extraction_confidence = _recover_extraction_confidence(conn, edge.source_event_id)
    subj_conf, obj_conf = _recover_mention_confidences(conn, edge)
    corroborating = count_corroborating_sources(
        conn,
        subject_id=edge.subject_id,
        predicate=edge.predicate,
        object_id=edge.object_id,
        exclude_event_id=edge.source_event_id,
    )
    source_conflicted = _source_is_conflicted(conn, event_hash)
    return EdgeConfidenceSignals(
        extraction_confidence=extraction_confidence,
        subject_mention_confidence=subj_conf,
        object_mention_confidence=obj_conf,
        predicate=edge.predicate,
        corroborating_sources=corroborating,
        source_conflicted=source_conflicted,
    )


def _source_event_hash(conn: sqlite3.Connection, source_event_id: str) -> str | None:
    row = conn.execute(
        "SELECT content_hash FROM events WHERE id = ?", (source_event_id,)
    ).fetchone()
    return row["content_hash"] if row is not None else None


def _recover_extraction_confidence(conn: sqlite3.Connection, source_event_id: str) -> float | None:
    """The extractor's whole-extraction self-assessment for the source event,
    from its latest ``extractor:%`` interpretation. None when unrecoverable."""
    row = conn.execute(
        "SELECT extraction FROM interpretations "
        "WHERE event_id = ? AND produced_by LIKE 'extractor:%' "
        "ORDER BY produced_at DESC, version DESC LIMIT 1",
        (source_event_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        extraction = json.loads(row["extraction"])
    except (ValueError, TypeError):
        return None
    raw = extraction.get("confidence") if isinstance(extraction, dict) else None
    return float(raw) if isinstance(raw, (int, float)) else None


def _recover_mention_confidences(
    conn: sqlite3.Connection, edge: EntityEdge
) -> tuple[float | None, float | None]:
    """The mention confidence for each endpoint in the edge's source event.

    Best-effort direct match on ``entity_id`` (the edge's endpoint ids are the
    mention ids from the same event's canonicalization). Unmatched → None,
    which the model treats as a neutral 0 for that endpoint."""
    rows = conn.execute(
        "SELECT entity_id, confidence FROM entity_mentions WHERE event_id = ?",
        (edge.source_event_id,),
    ).fetchall()
    by_entity = {r["entity_id"]: float(r["confidence"]) for r in rows}
    return by_entity.get(edge.subject_id), by_entity.get(edge.object_id)


def _source_is_conflicted(conn: sqlite3.Connection, event_hash: str | None) -> bool:
    """True when the edge's source event carries an unresolved conflict verdict.
    This is the main reason re-scoring exists — the conflict resolver runs
    AFTER the canonicalizer wrote the edge."""
    if event_hash is None:
        return False
    flags = read_conflicts_batch(conn, [event_hash]).get(event_hash) or []
    return any(is_unresolved_conflict(str(f.get("verdict", ""))) for f in flags)


def _row_to_edge(row: Any) -> EntityEdge:
    return EntityEdge(
        id=row["id"],
        subject_id=row["subject_id"],
        predicate=row["predicate"],
        object_id=row["object_id"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        discovered_at=row["discovered_at"],
        discovered_by=row["discovered_by"],
        source_event_id=row["source_event_id"],
        confidence=float(row["confidence"]),
    )
