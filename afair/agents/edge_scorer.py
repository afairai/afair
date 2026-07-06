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
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from ulid import ULID

from ..substrate import pipeline_events as pe
from ..substrate import watermarks
from ..substrate.belief import predicate_is_crisp, predicate_is_durable
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
from ..substrate.entities import (
    EntityEdge,
    count_corroborating_sources_batch,
    read_entity_by_id,
    resolve_canonical,
    write_edge_invalidation,
)
from .cold_path import ColdPathWorker
from .conflict_resolver import read_conflicts_batch
from .entity_canonicalizer import EDGE_EXEMPT_EVENT_KINDS
from .verdicts import is_unresolved_conflict

if TYPE_CHECKING:
    from ..settings import Settings

log = structlog.get_logger(__name__)

# Narrowed error set for a tunable-registry lookup fallback: a whitelist miss
# (KeyError), a DB hiccup (sqlite3.Error), or a malformed stored value
# (ValueError/TypeError from the float() coercion). A genuine programming bug
# (AttributeError, etc.) propagates instead of being silently swallowed.
_TUNABLE_FALLBACK_ERRORS = (KeyError, sqlite3.Error, ValueError, TypeError)


MAX_EDGES_PER_CYCLE = 100
"""Hard cap on edges scored per cycle. With 176 legacy edges the backfill
completes in two cycles; steady-state re-scoring is far smaller."""

EDGE_REVIEW_PROPOSAL_THRESHOLD = 0.6
"""Served confidence below which a live, unreviewed, `proposed` edge becomes a
candidate for the operator's review queue (ADR-0004 C4)."""

MAX_EDGE_REVIEW_PROPOSALS_PER_CYCLE = 3
"""Only the K lowest-confidence uncertain edges are queued per cycle —
quarantine research says queue only the uncertain so review effort stays
small. NOTE (review-fatigue behavior): the partial unique index
``proposed_corrections_open_unique`` (kind, entity_id) WHERE status='proposed'
means one OPEN edge-review proposal per SUBJECT entity at a time; a second
low-confidence edge on the same subject waits until the first is decided — and
the moment it IS decided, the next sub-threshold edge of that subject can queue
(calibration growth resumes on decide, not on prune). A decided edge itself
NEVER re-proposes: its durable guard is the append-only ``edge_reviews`` table
(the ``NOT EXISTS`` in _fetch_review_candidate_page), independent of whether the
old queue row still exists. The Pruner ages out decided edge_review queue rows
purely as hygiene."""

EDGE_REVIEW_CANDIDATE_POOL = 50
"""Page size for the keyset-paged review-candidate stream. Bounds the host
parameters per statement (replacing the old "load ALL live edges then build
IN(...) lists over the whole set", which exceeded SQLite's 32,766-variable limit
on a large graph and failed the cycle every 240s forever). This is a PAGE size,
not a hard candidate ceiling — ``_propose_edge_reviews`` keeps drawing pages via
keyset continuation until K proposals land or candidates are exhausted, so a
noisy entity monopolizing the lowest-confidence slots can't starve rank-N+1
subjects (ADR-0004 C4 scaling fix)."""

EDGE_SCORER_PRODUCED_BY = "edge_confidence_scorer:v0"

EDGE_EXPIRY_CONFIDENCE_THRESHOLD = 0.5
"""Served confidence below which a never-served edge is auto-expired. Aligned
with ``LOW_CONFIDENCE_EDGE_CAVEAT_THRESHOLD`` (handlers.py) — an edge recall
would only ever surface WITH a low-confidence caveat, and that never actually
got served, is noise worth retiring. Strictly below the 0.6 review threshold:
a served edge in [0.5, 0.6) is worth a human glance (queued), while an edge
under 0.5 that recall never even surfaced is not."""

EDGE_EXPIRY_MIN_AGE_DAYS = 14
"""Grace period before a never-served low-confidence edge can be auto-expired.
Load-bearing for rollout safety: on day one every historical edge is
technically "never served" (edge_serves starts empty), so without the grace a
first cycle would mass-expire the whole legacy graph. 14 days gives recall a
fair chance to surface a genuinely useful edge before the sweep touches it."""

MAX_EDGE_EXPIRIES_PER_CYCLE = 25
"""Hard cap on auto-expiries per cycle — bounds the blast radius of the sweep
(a bad threshold can only retire 25 edges before the operator notices the
``edges_expired_unserved`` stat), and keeps the write batch small."""

EDGE_AUTO_EXPIRE_PRODUCER = "edge_scorer:auto_expire:v1"

EDGE_SERVES_EPOCH_KEY = "edge_serves_epoch"
"""Marker key (in ``worker_watermarks``) recording WHEN serve-tracking began on
this vault. Rollout safety: ``edge_serves`` starts EMPTY at deploy, so every
pre-existing edge is vacuously "never served" — anchoring the auto-expiry grace
to ``discovered_at`` alone would mass-retire the whole legacy sub-0.5 tail
within hours of deploy, before recall ever had a serve-tracking window. The
never-served sweep anchors its grace to ``max(discovered_at, epoch)`` instead,
so every edge — however old its discovery — gets a full
``EDGE_EXPIRY_MIN_AGE_DAYS`` window of serve-tracking before it can be retired.
The epoch is set once, on the first scorer cycle that finds it absent, and never
moves (mutable derived state, not substrate — same footing as the P2a
watermarks)."""

MAX_NOISE_EXPIRIES_PER_CYCLE = 25
"""Hard cap on retro-sweep (B4) noise expiries per cycle — same blast-radius
discipline as the never-served sweep."""

TRANSIENT_NOISE_MIN_CONFIDENCE = 0.6
"""A source event must be classified ``transient`` with at least this confidence
before its edges are swept as noise — the same floor the write-time transient
gate (B2) uses, so a shaky 0.5-default classification can't retire a real edge."""

EDGE_NOISE_SWEEP_PRODUCER = "edge_scorer:noise_sweep:v1"


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
            # Corroboration once for the whole cycle (one indexed fetch +
            # resolve per DISTINCT predicate) instead of a full entity_edges
            # scan per edge.
            corroborating = count_corroborating_sources_batch(conn, edges)
            for edge in edges:
                new_conf, components = compute_edge_confidence(
                    _recover_signals(conn, edge, corroborating=corroborating.get(edge.id, 0)),
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

        # Auto-expire never-served low-confidence edges (serve-gated review).
        # Runs BEFORE proposal so an edge that qualifies for expiry this cycle
        # is out of the candidate set and never queued. Writes ONLY
        # edge_invalidations (append-only, I2) — NO edge_reviews row, so the
        # calibration set stays a pure record of operator verdicts.
        stats["edges_expired_unserved"] = _expire_unserved_low_confidence_edges(conn)

        # Retro-sweep noise edges (sub-batch B4): live, unreviewed edges whose
        # source is an observe event, or was classified confidently transient,
        # or whose predicate is non-durable. This drains the operator's existing
        # backlog created before the write-time gates (B1-B3) existed, and
        # catches transient-classified-later edges the write-time gate misses.
        # Same append-only invalidation, no edge_reviews row.
        stats["edges_expired_noise"] = _expire_noise_edges(conn)

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
                f"reviews_proposed={stats['edge_reviews_proposed']} "
                f"expired_unserved={stats['edges_expired_unserved']} "
                f"expired_noise={stats['edges_expired_noise']}"
            ),
        )
        return stats


# ── weight resolution (registry, S8) ───────────────────────────────────────


def _resolve_weights(conn: sqlite3.Connection) -> tuple[float, float]:
    """Resolve base_rate + corroboration_weight through the tuner registry,
    falling back to the module defaults (surprise-window pattern). A registry
    hiccup must never break scoring, so a narrowed error serves the pure-model
    defaults; a genuine bug propagates."""
    from ..substrate.confidence import DEFAULT_BASE_RATE, W_CORROBORATION

    base_rate = DEFAULT_BASE_RATE
    corroboration_weight = W_CORROBORATION
    try:
        from .tunable_registry import TunableRegistry

        registry = TunableRegistry(conn)
        base_rate = float(registry.get("edge_confidence", "base_rate"))
        corroboration_weight = float(registry.get("edge_confidence", "corroboration_weight"))
    except _TUNABLE_FALLBACK_ERRORS as exc:
        log.warning(
            "tunable_registry.fallback",
            worker="edge_scorer",
            tunable="edge_confidence.base_rate/corroboration_weight",
            error=str(exc),
        )
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


def _recover_signals(
    conn: sqlite3.Connection, edge: EntityEdge, *, corroborating: int
) -> EdgeConfidenceSignals:
    """Recover the edge-confidence signals from the substrate for a stored edge.

    ``corroborating`` is supplied by the caller from a single per-cycle
    ``count_corroborating_sources_batch`` pass rather than recomputed per edge
    (the old per-edge count full-scanned entity_edges). Every other signal
    degrades to None/0 gracefully (I3): a legacy edge whose extractor
    interpretation is unrecoverable still gets a sensible score from crispness +
    corroboration alone.
    """
    event_hash = _source_event_hash(conn, edge.source_event_id)
    extraction_confidence = _recover_extraction_confidence(conn, edge.source_event_id)
    subj_conf, obj_conf = _recover_mention_confidences(conn, edge)
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

    Selects live (non-invalidated), unreviewed, and ACTUALLY-SERVED edges
    (``EXISTS edge_serves`` — an edge recall never surfaced is not worth the
    operator's attention; that reversal is the core of the serve-gated review
    design) whose SERVED confidence is below the threshold — those resolve to
    `proposed` (served < threshold < the auto-confirm floor). Lowest confidence
    first. Each is inserted into
    ``proposed_corrections`` (kind ``edge_review``) with ``INSERT OR IGNORE``,
    which under the partial unique index ``proposed_corrections_open_unique``
    (one OPEN row per (kind, subject)) means a re-run never duplicates an open
    proposal and a second low-confidence edge on the same SUBJECT is absorbed
    until the first is DECIDED — at which point the slot frees and the next
    sub-threshold edge of that subject can queue. Returns the number of new
    proposals.

    Selection is done in SQL — a ``latest_scores`` CTE (ROW_NUMBER rn=1
    reproduces the batch helper's latest-score-wins) LEFT-JOINed with
    ``edge_invalidations`` (live only) and filtered by ``NOT EXISTS
    edge_reviews`` and ``COALESCE(latest_score, e.confidence) < threshold``
    (the column fallback). The old version loaded ALL live edges and built
    IN(...) lists over the whole set through the batch helpers — on a large
    graph that blew past SQLite's 32,766-variable limit and failed the cycle
    every 240s forever.

    To bound the host parameters per statement WITHOUT starving subjects, the
    candidate stream is PAGED via keyset continuation on
    ``(served_confidence, discovered_at, id)`` in bounded pages of
    ``EDGE_REVIEW_CANDIDATE_POOL``. This restores the old walk-until-K
    semantics: if the lowest-confidence edges all share a few subjects and get
    absorbed by ``UNIQUE(kind, subject)``, paging keeps drawing until K distinct
    subjects land or the candidates are exhausted — so a noisy entity with many
    sub-threshold edges can never permanently starve rank-N+1 subjects. (The
    subject stored on a proposal is the merge-RESOLVED id, which is why the
    exclusion can't be pushed into SQL as ``NOT EXISTS proposed_corrections ...
    entity_id = e.subject_id`` — ``e.subject_id`` is raw, so merged subjects
    would slip through. INSERT OR IGNORE on the resolved subject is the
    authority.)
    """
    proposed = 0
    # Keyset cursor: (served_confidence, discovered_at, id) of the last row of
    # the previous page. Strictly increasing on id (edge PK) → no skips/dups.
    cursor: tuple[float, str, str] | None = None
    while proposed < MAX_EDGE_REVIEW_PROPOSALS_PER_CYCLE:
        page = _fetch_review_candidate_page(conn, cursor, EDGE_REVIEW_CANDIDATE_POOL)
        if not page:
            break
        edges = [_row_to_edge(r) for r in page]
        served = {r["id"]: float(r["served_confidence"]) for r in page}
        # Bounded (<= page) components fetch for the evidence string.
        scores = latest_edge_scores_batch(conn, [e.id for e in edges])
        for edge in edges:
            conf = served[edge.id]
            components = scores[edge.id].components if edge.id in scores else {}
            if _insert_edge_review_proposal(
                conn, edge=edge, confidence=conf, components=components
            ):
                proposed += 1
                if proposed >= MAX_EDGE_REVIEW_PROPOSALS_PER_CYCLE:
                    break
        last = page[-1]
        cursor = (float(last["served_confidence"]), last["discovered_at"], last["id"])
        if len(page) < EDGE_REVIEW_CANDIDATE_POOL:
            break  # last page — candidates exhausted
    return proposed


def _fetch_review_candidate_page(
    conn: sqlite3.Connection,
    cursor: tuple[float, str, str] | None,
    page_size: int,
) -> list[Any]:
    """One keyset page of low-confidence, live, unreviewed edge candidates,
    ordered ``(served_confidence, discovered_at, id)`` ascending.

    ``cursor`` is the last row of the previous page; None for the first page.
    The keyset predicate repeats the ``COALESCE`` expression (a SELECT alias
    isn't visible in WHERE) as a row-value comparison so the continuation is
    exact and bound-parameter-free per page.
    """
    sql = """
        WITH latest_scores AS (
            SELECT edge_id, confidence,
                   ROW_NUMBER() OVER (
                       PARTITION BY edge_id
                       ORDER BY computed_at DESC, id DESC
                   ) AS rn
            FROM edge_confidence_scores
        )
        SELECT e.*, COALESCE(ls.confidence, e.confidence) AS served_confidence
        FROM entity_edges e
        LEFT JOIN edge_invalidations i ON i.edge_id = e.id
        LEFT JOIN latest_scores ls ON ls.edge_id = e.id AND ls.rn = 1
        WHERE i.id IS NULL
          AND NOT EXISTS (SELECT 1 FROM edge_reviews r WHERE r.edge_id = e.id)
          AND EXISTS (SELECT 1 FROM edge_serves sv WHERE sv.edge_id = e.id)
          AND COALESCE(ls.confidence, e.confidence) < ?
    """
    params: list[Any] = [EDGE_REVIEW_PROPOSAL_THRESHOLD]
    if cursor is not None:
        sql += " AND (COALESCE(ls.confidence, e.confidence), e.discovered_at, e.id) > (?, ?, ?)"
        params.extend(cursor)
    sql += " ORDER BY served_confidence ASC, e.discovered_at ASC, e.id ASC LIMIT ?"
    params.append(page_size)
    return conn.execute(sql, params).fetchall()


def _edge_age_days(discovered_at: str) -> int:
    """Whole days between an edge's ``discovered_at`` and now (UTC), floored at
    0. Naive timestamps are read as UTC (the substrate writes tz-aware ISO, but
    be defensive)."""
    try:
        dt = datetime.fromisoformat(discovered_at)
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return max(0, int((datetime.now(UTC) - dt).total_seconds() // 86400))


def _serve_tracking_epoch(conn: sqlite3.Connection) -> str:
    """The ISO timestamp when serve-tracking began on this vault, set once and
    never moved. Read-or-create against ``worker_watermarks`` (mutable derived
    state). ``INSERT OR IGNORE`` + re-read makes concurrent first-cycles safe."""
    existing = watermarks.read_watermark(conn, EDGE_SERVES_EPOCH_KEY)
    if existing is not None:
        return existing[0]  # through_created_at carries the epoch
    now = datetime.now(UTC).isoformat()
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO worker_watermarks "
            "(worker, through_created_at, through_id, updated_at) VALUES (?, ?, ?, ?)",
            (EDGE_SERVES_EPOCH_KEY, now, str(ULID()), now),
        )
    row = watermarks.read_watermark(conn, EDGE_SERVES_EPOCH_KEY)
    return row[0] if row is not None else now


def _expire_unserved_low_confidence_edges(conn: sqlite3.Connection) -> int:
    """Auto-expire edges that recall never served and that sit below the expiry
    confidence floor, once they are past the serve-tracking grace.

    An edge earns retirement only when ALL hold: live (not already
    invalidated), unreviewed (the operator never touched it — reviewed edges
    are entrenched, ADR-0002), NEVER served (no ``edge_serves`` row), served
    confidence < ``EDGE_EXPIRY_CONFIDENCE_THRESHOLD`` (0.5), and past the grace.
    Capped at ``MAX_EDGE_EXPIRIES_PER_CYCLE`` (25) per run.

    ROLLOUT SAFETY — the grace is anchored to ``max(discovered_at, epoch)``,
    NOT to ``discovered_at`` alone. ``edge_serves`` starts empty at deploy, so
    every legacy edge is vacuously never-served; a discovered_at-only grace
    would mass-retire the entire pre-deploy sub-0.5 tail in the first cycles
    (it is all old), overriding ADR-0004's deliberate "serve-with-caveat, not
    suppress" decision — and an invalidation has no un-reject path. Anchoring to
    the serve-tracking epoch gives every edge a full ``EDGE_EXPIRY_MIN_AGE_DAYS``
    window of actual serve-tracking before it can be retired, however old its
    discovery. On a fresh deploy the epoch is ~now, so this sweep expires nothing
    until serve-tracking has run for the grace period.

    Retirement is an append-only ``edge_invalidations`` row (I2 — never an
    UPDATE/DELETE of the edge; the edge and its interpretation stay as history,
    I3, re-derivable under a version bump). No ``edge_reviews`` row is written,
    so the calibration report stays a pure record of operator verdicts.
    Idempotent across cycles: an invalidated edge fails the ``i.id IS NULL``
    filter next time, so it is chosen at most once (the append-only invalidation
    with ``source_event_id=None`` is itself producer-tagged + reasoned, I7)."""
    epoch = _serve_tracking_epoch(conn)
    cutoff = (datetime.now(UTC) - timedelta(days=EDGE_EXPIRY_MIN_AGE_DAYS)).isoformat()
    rows = conn.execute(
        """
        WITH latest_scores AS (
            SELECT edge_id, confidence,
                   ROW_NUMBER() OVER (
                       PARTITION BY edge_id
                       ORDER BY computed_at DESC, id DESC
                   ) AS rn
            FROM edge_confidence_scores
        )
        SELECT e.id, e.discovered_at,
               COALESCE(ls.confidence, e.confidence) AS served_confidence
        FROM entity_edges e
        LEFT JOIN edge_invalidations i ON i.edge_id = e.id
        LEFT JOIN latest_scores ls ON ls.edge_id = e.id AND ls.rn = 1
        WHERE i.id IS NULL
          AND NOT EXISTS (SELECT 1 FROM edge_reviews r WHERE r.edge_id = e.id)
          AND NOT EXISTS (SELECT 1 FROM edge_serves sv WHERE sv.edge_id = e.id)
          AND COALESCE(ls.confidence, e.confidence) < ?
          AND max(e.discovered_at, ?) < ?
        ORDER BY served_confidence ASC, e.discovered_at ASC, e.id ASC
        LIMIT ?
        """,
        (EDGE_EXPIRY_CONFIDENCE_THRESHOLD, epoch, cutoff, MAX_EDGE_EXPIRIES_PER_CYCLE),
    ).fetchall()
    expired = 0
    for r in rows:
        conf = float(r["served_confidence"])
        age = _edge_age_days(r["discovered_at"])
        result = write_edge_invalidation(
            conn,
            edge_id=r["id"],
            invalidated_by=EDGE_AUTO_EXPIRE_PRODUCER,
            reason=f"auto-expired: never served, confidence {conf:.2f}, age {age}d",
            source_event_id=None,
        )
        if result is not None:
            expired += 1
    return expired


def _noise_reason(row: Any) -> str | None:
    """The reason an edge is noise (B4), or None if it is a keeper.

    Priority: observe-sourced → transient-sourced (confident) → non-durable
    predicate. Producer-neutral phrasing so the invalidation reason reads well
    in an audit."""
    if row["source_kind"] in EDGE_EXEMPT_EVENT_KINDS:
        return "noise sweep: relation derived from an observe event"
    tconf = row["tconf"]
    if (
        row["tclass"] == "transient"
        and tconf is not None
        and float(tconf) >= TRANSIENT_NOISE_MIN_CONFIDENCE
    ):
        return f"noise sweep: transient source (confidence {float(tconf):.2f})"
    if not predicate_is_durable(row["predicate"]):
        return f"noise sweep: non-durable predicate {row['predicate']!r}"
    return None


def _fetch_noise_candidate_page(
    conn: sqlite3.Connection,
    cursor: tuple[str, str] | None,
    page_size: int,
) -> list[Any]:
    """One keyset page of live, unreviewed edges with the signals the noise
    classifier needs (source-event kind + latest temporal class/confidence),
    ordered ``(discovered_at, id)`` ascending.

    Reviewed edges are excluded (``NOT EXISTS edge_reviews``) — an edge the
    operator touched is entrenched (ADR-0002) and never swept. Paged so the
    scan stays bounded per statement; the caller walks pages until the cap or
    exhaustion, and expired edges leave the set (``i.id IS NULL``)."""
    sql = """
        WITH latest_temporal AS (
            SELECT event_hash, temporal_class, confidence,
                   ROW_NUMBER() OVER (
                       PARTITION BY event_hash
                       ORDER BY created_at DESC, id DESC
                   ) AS rn
            FROM event_temporal
        )
        SELECT e.id, e.predicate, e.discovered_at,
               ev.kind AS source_kind,
               lt.temporal_class AS tclass, lt.confidence AS tconf
        FROM entity_edges e
        LEFT JOIN edge_invalidations i ON i.edge_id = e.id
        LEFT JOIN events ev ON ev.id = e.source_event_id
        LEFT JOIN latest_temporal lt ON lt.event_hash = ev.content_hash AND lt.rn = 1
        WHERE i.id IS NULL
          AND NOT EXISTS (SELECT 1 FROM edge_reviews r WHERE r.edge_id = e.id)
    """
    params: list[Any] = []
    if cursor is not None:
        sql += " AND (e.discovered_at, e.id) > (?, ?)"
        params.extend(cursor)
    sql += " ORDER BY e.discovered_at ASC, e.id ASC LIMIT ?"
    params.append(page_size)
    return conn.execute(sql, params).fetchall()


def _expire_noise_edges(conn: sqlite3.Connection) -> int:
    """Retro-sweep noise edges: live, unreviewed edges that are observe-sourced,
    confidently-transient-sourced, or non-durable-predicated (sub-batch B4).

    Each is retired with an append-only ``edge_invalidations`` row (I2 — the
    edge + its interpretation stay as history, I3, re-derivable under a version
    bump). No ``edge_reviews`` row is written. Capped at
    ``MAX_NOISE_EXPIRIES_PER_CYCLE`` per run; keyset-paged so a large graph is
    scanned in bounded statements and forward progress is guaranteed (an
    expired edge drops out via ``i.id IS NULL`` and the cursor never revisits
    a keeper). Never touches an edge with an ``edge_reviews`` row — an
    operator-decided edge is entrenched (ADR-0002)."""
    expired = 0
    cursor: tuple[str, str] | None = None
    while expired < MAX_NOISE_EXPIRIES_PER_CYCLE:
        page = _fetch_noise_candidate_page(conn, cursor, EDGE_REVIEW_CANDIDATE_POOL)
        if not page:
            break
        for row in page:
            reason = _noise_reason(row)
            if reason is None:
                continue
            result = write_edge_invalidation(
                conn,
                edge_id=row["id"],
                invalidated_by=EDGE_NOISE_SWEEP_PRODUCER,
                reason=reason,
                source_event_id=None,
            )
            if result is not None:
                expired += 1
                if expired >= MAX_NOISE_EXPIRIES_PER_CYCLE:
                    break
        last = page[-1]
        cursor = (last["discovered_at"], last["id"])
        if len(page) < EDGE_REVIEW_CANDIDATE_POOL:
            break  # last page — candidates exhausted
    return expired


def _insert_edge_review_proposal(
    conn: sqlite3.Connection,
    *,
    edge: EntityEdge,
    confidence: float,
    components: dict[str, Any],
) -> bool:
    """Insert one edge-review proposal (INSERT OR IGNORE). Returns True when a
    new row landed, False when the partial unique index (one OPEN row per
    (kind, subject)) absorbed it."""
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
