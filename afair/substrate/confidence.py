"""Edge-confidence model (ADR-0004). Pure logic: callers pass signals in.

The 176 legacy ``entity_edges`` rows all carry a hardcoded ``confidence = 0.8``
that means nothing. This module replaces that constant with a transparent,
explainable score computed from signals available at edge-discovery time (and
recomputable later as post-write signals accumulate).

The score is a log-odds (logit-space) sum of named terms. Every missing signal
contributes exactly 0 (the terms are deviations from a neutral point), so the
model degrades gracefully: an edge whose extraction row is unrecoverable still
gets a sensible score from crispness + corroboration alone. The full per-term
breakdown is returned alongside the score and stored next to it, so "why 0.63?"
always has an answer.

The SCORING model is pure (no DB, no LLM, no I/O) — it mirrors ``belief.py``'s
style; the cold-path scorer (``agents/edge_scorer.py``) and the canonicalizer
recover the signals from the substrate and call in here. The one exception is
:func:`calibration_report`, a measurement helper at the bottom that reads the
operator's ``edge_reviews`` verdicts to check how well the priors match reality
— it takes a connection because the ground truth lives in the substrate.
"""

from __future__ import annotations

from math import exp, log, log2
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from .belief import predicate_is_crisp
from .edge_confidence import latest_edge_confidence_batch

if TYPE_CHECKING:
    import sqlite3

EDGE_CONFIDENCE_VERSION = "edge_confidence:v1"
"""Stamped into edge_confidence_scores.computed_by. Bump to re-derive (I7)."""

# Hand-set anchors (ADR-0004 "Calibration"). base_rate and corroboration
# weight are ALSO tuner-whitelisted (S8); these constants are the defaults.
DEFAULT_BASE_RATE = 0.70
"""Prior probability an evidence-gated agent-derived edge is true — the
calibration intercept. Tuner-whitelisted as ``edge_confidence.base_rate``."""

W_EXTRACT = 1.5
"""Log-odds weight on the extractor's whole-extraction self-assessment,
measured as a deviation from EXTRACTION_NEUTRAL."""

EXTRACTION_NEUTRAL = 0.7
"""The extraction confidence that contributes zero — a self-assessment above
this raises the score, below it lowers it."""

W_CRISP = 0.4
"""Log-odds bonus/penalty for a crisp vs vague predicate — the ADR-0002
confabulation tell (a real relation is a short verb-phrase)."""

W_MENTION = 2.0
"""Log-odds weight on the WEAKEST endpoint mention (min over the two ends),
measured as a deviation from a perfect 1.0 exact match. Mirrors the AGM rule
that a belief is never more entrenched than its least-entrenched
justification, so the term is always <= 0."""

W_CORROBORATION = 0.8
"""Log-odds added per doubling of independent corroborating source events.
Tuner-whitelisted as ``edge_confidence.corroboration_weight``."""

W_CONFLICT = 1.0
"""Log-odds subtracted when the edge's source event carries an unresolved
conflict verdict — a contested source drops a strong edge below the
auto-confirm floor."""

MIN_EDGE_CONFIDENCE = 0.05
MAX_EDGE_CONFIDENCE = 0.99
"""Clamp bounds. The upper clamp keeps an agent-derived belief from ever being
served as certain fact, in line with the recall honesty layer."""

_BASE_RATE_MIN = 0.01
_BASE_RATE_MAX = 0.99
"""Defensive clamp on the base_rate input before logit — logit(0) and logit(1)
are undefined, so a promoted/misconfigured base_rate can never blow up."""


class EdgeConfidenceSignals(BaseModel):
    """The inputs to :func:`compute_edge_confidence`.

    Every optional signal is ``None`` when unavailable (recoverable-from-nothing
    legacy edges), and each ``None`` contributes exactly 0 to the score.
    """

    extraction_confidence: float | None = None
    """The extractor's whole-extraction self-assessment (``extraction['confidence']``)."""

    subject_mention_confidence: float | None = None
    """The mention confidence the canonicalizer wrote for the subject endpoint
    (exact=1.0, alias=0.9, llm=verdict.confidence, new=0.5)."""

    object_mention_confidence: float | None = None
    """The mention confidence for the object endpoint (same scale)."""

    predicate: str
    """The edge predicate — its crispness is the ADR-0002 confabulation tell."""

    corroborating_sources: int = 0
    """Count of OTHER live edges asserting the same canonical triple from
    distinct source events."""

    source_conflicted: bool = False
    """True when the edge's source event carries an unresolved conflict verdict."""


def _logit(p: float) -> float:
    """Inverse sigmoid — maps a probability in (0, 1) to log-odds."""
    p = min(_BASE_RATE_MAX, max(_BASE_RATE_MIN, p))
    return log(p / (1.0 - p))


def _sigmoid(z: float) -> float:
    """Logistic squash — maps log-odds back to a probability in (0, 1)."""
    if z >= 0:
        return 1.0 / (1.0 + exp(-z))
    # Numerically stable form for large-negative z (avoids exp overflow).
    ez = exp(z)
    return ez / (1.0 + ez)


def _clamp_confidence(value: float) -> float:
    return min(MAX_EDGE_CONFIDENCE, max(MIN_EDGE_CONFIDENCE, value))


def compute_edge_confidence(
    signals: EdgeConfidenceSignals,
    *,
    base_rate: float = DEFAULT_BASE_RATE,
    corroboration_weight: float = W_CORROBORATION,
) -> tuple[float, dict[str, Any]]:
    """Compute a served confidence in [MIN, MAX] plus its full explanation.

    The score is a sum of named log-odds terms (each a deviation from a neutral
    point), squashed through a sigmoid and clamped. Returns
    ``(confidence, components)`` where ``components`` records every signal,
    every per-term contribution, and the summed ``z`` — enough to recompute the
    score and to answer "why this number?".

    ``base_rate`` and ``corroboration_weight`` are passed in so the tuner-
    resolved values (S8) flow through; they default to the module constants.
    """
    base_term = _logit(base_rate)
    z = base_term

    # Extraction confidence — deviation from EXTRACTION_NEUTRAL, skipped if None.
    if signals.extraction_confidence is not None:
        extract_term = W_EXTRACT * (signals.extraction_confidence - EXTRACTION_NEUTRAL)
    else:
        extract_term = 0.0
    z += extract_term

    # Predicate crispness — bonus for a crisp relation, penalty for vague.
    crisp_term = W_CRISP if predicate_is_crisp(signals.predicate) else -W_CRISP
    z += crisp_term

    # Weakest-endpoint mention — deviation of the WEAKER end from a perfect 1.0.
    mention_values = [
        m
        for m in (signals.subject_mention_confidence, signals.object_mention_confidence)
        if m is not None
    ]
    mention_term = W_MENTION * (min(mention_values) - 1.0) if mention_values else 0.0
    z += mention_term

    # Corroboration — log-odds per doubling of independent corroborating sources.
    corroboration_term = corroboration_weight * log2(1 + max(0, signals.corroborating_sources))
    z += corroboration_term

    # Conflict — a contested source drops the score.
    conflict_term = -W_CONFLICT if signals.source_conflicted else 0.0
    z += conflict_term

    confidence = _clamp_confidence(_sigmoid(z))
    components: dict[str, Any] = {
        "version": EDGE_CONFIDENCE_VERSION,
        "signals": signals.model_dump(),
        "weights": {
            "base_rate": base_rate,
            "w_extract": W_EXTRACT,
            "extraction_neutral": EXTRACTION_NEUTRAL,
            "w_crisp": W_CRISP,
            "w_mention": W_MENTION,
            "corroboration_weight": corroboration_weight,
            "w_conflict": W_CONFLICT,
        },
        "terms": {
            "base": base_term,
            "extract": extract_term,
            "crisp": crisp_term,
            "mention": mention_term,
            "corroboration": corroboration_term,
            "conflict": conflict_term,
        },
        "z": z,
    }
    return confidence, components


# ── calibration (ADR-0004 "Calibration") ───────────────────────────────────

CALIBRATION_MIN_REVIEWS = 20
"""Below this many labeled edges (with >= 5 in each class) the calibration
report is "insufficient" and nothing moves — the bootstrap needs a real sample
before the numbers mean anything."""

_CALIBRATION_BUCKETS: tuple[tuple[float, float], ...] = (
    (0.0, 0.25),
    (0.25, 0.5),
    (0.5, 0.75),
    (0.75, 1.0),
)


def calibration_report(conn: sqlite3.Connection) -> dict[str, Any]:
    """Measure how well served confidences match the operator's verdicts.

    Joins the latest ``edge_reviews`` verdict per edge with that edge's SERVED
    confidence (latest score, else the stored column) and reports, per
    confidence bucket, the observed confirm-rate vs the mean predicted
    confidence, plus an overall Brier score. This is the calibration target the
    ADR names: an edge served at 0.9 should be confirmed ~90% of the time.

    Returns ``sufficient=False`` (and moves nothing) until there are at least
    ``CALIBRATION_MIN_REVIEWS`` reviewed edges with >= 5 in each class. Pure
    read — no writes, no LLM.
    """
    verdict_rows = conn.execute(
        "SELECT edge_id, verdict FROM edge_reviews ORDER BY reviewed_at ASC, id ASC"
    ).fetchall()
    # Ascending overwrite → latest verdict per edge.
    latest_verdict = {r["edge_id"]: r["verdict"] for r in verdict_rows}
    empty: dict[str, Any] = {
        "reviewed": 0,
        "confirmed": 0,
        "rejected": 0,
        "brier": None,
        "buckets": [],
        "sufficient": False,
    }
    if not latest_verdict:
        return empty

    edge_ids = list(latest_verdict)
    served = latest_edge_confidence_batch(conn, edge_ids)
    placeholders = ",".join("?" * len(edge_ids))
    col_rows = conn.execute(
        f"SELECT id, confidence FROM entity_edges WHERE id IN ({placeholders})",
        edge_ids,
    ).fetchall()
    column = {r["id"]: float(r["confidence"]) for r in col_rows}

    bucket_state = [
        {"lo": lo, "hi": hi, "n": 0, "confirmed": 0, "sum_pred": 0.0}
        for lo, hi in _CALIBRATION_BUCKETS
    ]
    confirmed = 0
    rejected = 0
    brier_sum = 0.0
    scored = 0
    for edge_id, verdict in latest_verdict.items():
        if verdict not in ("confirm", "reject"):
            continue
        predicted = served.get(edge_id, column.get(edge_id))
        if predicted is None:
            continue  # edge row gone (shouldn't happen; defensive)
        outcome = 1.0 if verdict == "confirm" else 0.0
        if verdict == "confirm":
            confirmed += 1
        else:
            rejected += 1
        brier_sum += (predicted - outcome) ** 2
        scored += 1
        for i, (lo, hi) in enumerate(_CALIBRATION_BUCKETS):
            # Top bucket is closed on the right so a served 0.99 lands in it.
            in_bucket = lo <= predicted < hi or (
                i == len(_CALIBRATION_BUCKETS) - 1 and predicted <= hi
            )
            if in_bucket:
                bucket_state[i]["n"] += 1
                bucket_state[i]["confirmed"] += int(outcome)
                bucket_state[i]["sum_pred"] += predicted
                break

    buckets = [
        {
            "lo": b["lo"],
            "hi": b["hi"],
            "n": b["n"],
            "confirm_rate": (b["confirmed"] / b["n"]) if b["n"] else None,
            "mean_predicted": (b["sum_pred"] / b["n"]) if b["n"] else None,
        }
        for b in bucket_state
    ]
    return {
        "reviewed": scored,
        "confirmed": confirmed,
        "rejected": rejected,
        "brier": (brier_sum / scored) if scored else None,
        "buckets": buckets,
        "sufficient": scored >= CALIBRATION_MIN_REVIEWS and confirmed >= 5 and rejected >= 5,
    }
