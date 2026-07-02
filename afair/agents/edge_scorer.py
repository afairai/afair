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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from ulid import ULID

from ..substrate import pipeline_events as pe
from ..substrate.belief import predicate_is_crisp
from ..substrate.confidence import (
    EDGE_CONFIDENCE_VERSION,
    EdgeConfidenceSignals,
    calibration_report,
    compute_edge_confidence,
)
from ..substrate.edge_confidence import (
    EDGE_CONFIDENCE_EPSILON,
    latest_edge_confidence_batch,
    latest_edge_scores_batch,
    write_edge_confidence_score,
)
from ..substrate.entities import (
    EntityEdge,
    count_corroborating_sources,
    latest_edge_reviews_batch,
    read_entity_by_id,
    resolve_canonical,
)
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

EDGE_REVIEW_PROPOSAL_THRESHOLD = 0.6
"""Served confidence below which a live, unreviewed, `proposed` edge becomes a
candidate for the operator's review queue (ADR-0004 C4)."""

MAX_EDGE_REVIEW_PROPOSALS_PER_CYCLE = 3
"""Only the K lowest-confidence uncertain edges are queued per cycle —
quarantine research says queue only the uncertain so review effort stays
small. NOTE (review-fatigue behavior): UNIQUE(kind, entity_id) means one open
edge-review proposal per SUBJECT entity at a time; a second low-confidence edge
on the same subject waits until the first is decided (the pruner clears applied
rows)."""

EDGE_SCORER_PRODUCED_BY = "edge_confidence_scorer:v0"


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

        # Propose the lowest-confidence uncertain edges for operator review
        # (ADR-0004 C4). This gives record_edge_review its first production
        # caller and makes the calibration set grow.
        stats["edge_reviews_proposed"] = _propose_edge_reviews(conn)

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
            producer=EDGE_SCORER_PRODUCED_BY,
            detail=(
                f"scored={stats['edges_scored']} "
                f"skipped_unchanged={stats['edges_skipped_unchanged']} "
                f"legacy_backfilled={stats['legacy_backfilled']} "
                f"reviews_proposed={stats['edge_reviews_proposed']}"
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


# ── edge-review proposals (ADR-0004 C4) ─────────────────────────────────────


def _propose_edge_reviews(conn: sqlite3.Connection) -> int:
    """Queue the K lowest-confidence uncertain edges for operator review.

    Selects live (non-invalidated), unreviewed edges whose SERVED confidence is
    below the threshold — those resolve to `proposed` (served < threshold <
    the auto-confirm floor). Lowest confidence first, capped per cycle. Each is
    inserted into ``proposed_corrections`` (kind ``edge_review``) with
    ``INSERT OR IGNORE`` on the UNIQUE(kind, entity_id), so a re-run never
    duplicates an open proposal. Returns the number of new proposals.
    """
    live_rows = conn.execute(
        """
        SELECT e.* FROM entity_edges e
        LEFT JOIN edge_invalidations i ON i.edge_id = e.id
        WHERE i.id IS NULL
        """
    ).fetchall()
    if not live_rows:
        return 0
    edges = [_row_to_edge(r) for r in live_rows]
    ids = [e.id for e in edges]
    served = latest_edge_confidence_batch(conn, ids)
    reviewed = latest_edge_reviews_batch(conn, ids)
    scores = latest_edge_scores_batch(conn, ids)

    candidates: list[tuple[float, EntityEdge]] = []
    for edge in edges:
        if edge.id in reviewed:
            continue  # already has an operator verdict
        conf = served.get(edge.id, edge.confidence)
        if conf >= EDGE_REVIEW_PROPOSAL_THRESHOLD:
            continue  # confident enough — not a review candidate
        candidates.append((conf, edge))
    candidates.sort(key=lambda c: c[0])

    proposed = 0
    for conf, edge in candidates:
        if proposed >= MAX_EDGE_REVIEW_PROPOSALS_PER_CYCLE:
            break
        components = scores[edge.id].components if edge.id in scores else {}
        if _insert_edge_review_proposal(conn, edge=edge, confidence=conf, components=components):
            proposed += 1
    return proposed


def _insert_edge_review_proposal(
    conn: sqlite3.Connection,
    *,
    edge: EntityEdge,
    confidence: float,
    components: dict[str, Any],
) -> bool:
    """Insert one edge-review proposal (INSERT OR IGNORE). Returns True when a
    new row landed, False when the UNIQUE(kind, subject) absorbed it."""
    subject_id = resolve_canonical(conn, edge.subject_id)
    object_id = resolve_canonical(conn, edge.object_id)
    subj = read_entity_by_id(conn, subject_id)
    obj = read_entity_by_id(conn, object_id)
    subject_name = subj.canonical_name if subj is not None else subject_id
    object_name = obj.canonical_name if obj is not None else object_id
    detail = {
        "edge_id": edge.id,
        "subject_name": subject_name,
        "predicate": edge.predicate,
        "object_name": object_name,
        "confidence": round(confidence, 3),
        "source_event_id": edge.source_event_id,
    }
    evidence = _proposal_evidence(edge, confidence, components)
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO proposed_corrections (
            id, kind, entity_id, detail, evidence, confidence, tier,
            detected_by, detected_at, status
        ) VALUES (?, 'edge_review', ?, ?, ?, ?, 'review', ?, ?, 'proposed')
        """,
        (
            str(ULID()),
            subject_id,
            json.dumps(detail, ensure_ascii=False, sort_keys=True),
            evidence,
            confidence,
            EDGE_SCORER_PRODUCED_BY,
            datetime.now(UTC).isoformat(),
        ),
    )
    conn.commit()
    return cur.rowcount > 0


def _proposal_evidence(edge: EntityEdge, confidence: float, components: dict[str, Any]) -> str:
    """A short, human-readable reason string built from the stored components —
    e.g. ``"served confidence 0.42 (vague predicate, new endpoint, no
    corroboration)"``."""
    reasons: list[str] = []
    signals = components.get("signals", {}) if isinstance(components, dict) else {}
    if not predicate_is_crisp(edge.predicate):
        reasons.append("vague predicate")
    mentions = [
        m
        for m in (
            signals.get("subject_mention_confidence"),
            signals.get("object_mention_confidence"),
        )
        if isinstance(m, (int, float))
    ]
    if mentions and min(mentions) <= 0.5:
        reasons.append("new endpoint")
    if signals.get("corroborating_sources", 0) == 0:
        reasons.append("no corroboration")
    if signals.get("source_conflicted"):
        reasons.append("contested source")
    tail = f" ({', '.join(reasons)})" if reasons else ""
    return f"served confidence {confidence:.2f}{tail}"
