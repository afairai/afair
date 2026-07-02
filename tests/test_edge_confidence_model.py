"""Pure-model tests for the edge-confidence formula (ADR-0004 / S1).

No DB, no LLM — just the deterministic log-odds computation and its stored
explanation. The anchor points are the ADR's "Sane anchor points" section:
a well-grounded edge lands near the historical 0.8, a weak one falls below the
auto-confirm floor, corroboration raises, a contested source drops.
"""

from __future__ import annotations

from math import isclose

from afair.substrate.confidence import (
    DEFAULT_BASE_RATE,
    EDGE_CONFIDENCE_VERSION,
    MAX_EDGE_CONFIDENCE,
    MIN_EDGE_CONFIDENCE,
    EdgeConfidenceSignals,
    _logit,
    _sigmoid,
    compute_edge_confidence,
)


def test_strong_edge_lands_near_historical_0_8() -> None:
    signals = EdgeConfidenceSignals(
        extraction_confidence=0.9,
        subject_mention_confidence=1.0,
        object_mention_confidence=1.0,
        predicate="runs",
        corroborating_sources=0,
    )
    c, _ = compute_edge_confidence(signals)
    assert 0.78 <= c <= 0.86


def test_weak_edge_falls_below_auto_confirm_floor() -> None:
    signals = EdgeConfidenceSignals(
        extraction_confidence=None,
        subject_mention_confidence=0.5,
        object_mention_confidence=None,
        predicate="is tech person in circle of",  # 5 words → not crisp
        corroborating_sources=0,
    )
    c, _ = compute_edge_confidence(signals)
    assert c < 0.5


def test_corroboration_raises() -> None:
    base = EdgeConfidenceSignals(
        extraction_confidence=0.9,
        subject_mention_confidence=1.0,
        object_mention_confidence=1.0,
        predicate="runs",
        corroborating_sources=0,
    )
    two = base.model_copy(update={"corroborating_sources": 2})
    c0, _ = compute_edge_confidence(base)
    c2, _ = compute_edge_confidence(two)
    assert c2 > c0  # monotone increase
    assert c2 > 0.9


def test_conflict_penalty_drops_below_floor() -> None:
    signals = EdgeConfidenceSignals(
        extraction_confidence=0.9,
        subject_mention_confidence=1.0,
        object_mention_confidence=1.0,
        predicate="runs",
        corroborating_sources=0,
        source_conflicted=True,
    )
    c, _ = compute_edge_confidence(signals)
    assert c < 0.75


def test_all_signals_missing_is_neutral_plus_crispness_only() -> None:
    # Crisp predicate, nothing else: z == logit(base) + W_CRISP exactly.
    crisp = EdgeConfidenceSignals(predicate="runs")
    c_crisp, _comp_crisp = compute_edge_confidence(crisp)
    assert MIN_EDGE_CONFIDENCE < c_crisp < MAX_EDGE_CONFIDENCE
    from afair.substrate.confidence import W_CRISP

    assert isclose(c_crisp, _sigmoid(_logit(DEFAULT_BASE_RATE) + W_CRISP), rel_tol=1e-12)

    # Vague predicate, nothing else: z == logit(base) - W_CRISP exactly.
    vague = EdgeConfidenceSignals(predicate="is a person in the circle of")
    c_vague, _ = compute_edge_confidence(vague)
    assert isclose(c_vague, _sigmoid(_logit(DEFAULT_BASE_RATE) - W_CRISP), rel_tol=1e-12)


def test_clamps() -> None:
    # Absurd corroboration never exceeds MAX.
    high = EdgeConfidenceSignals(
        extraction_confidence=1.0,
        subject_mention_confidence=1.0,
        object_mention_confidence=1.0,
        predicate="runs",
        corroborating_sources=1000,
    )
    c_high, _ = compute_edge_confidence(high)
    assert c_high == MAX_EDGE_CONFIDENCE

    # Heavy penalties never go below MIN.
    low = EdgeConfidenceSignals(
        extraction_confidence=0.0,
        subject_mention_confidence=0.0,
        object_mention_confidence=0.0,
        predicate="is a very long vague profile phrase indeed",
        corroborating_sources=0,
        source_conflicted=True,
    )
    c_low, _ = compute_edge_confidence(low)
    assert c_low == MIN_EDGE_CONFIDENCE


def test_components_are_complete_and_reproducible() -> None:
    signals = EdgeConfidenceSignals(
        extraction_confidence=0.85,
        subject_mention_confidence=0.9,
        object_mention_confidence=0.5,
        predicate="collaborates with",
        corroborating_sources=1,
        source_conflicted=False,
    )
    c, comp = compute_edge_confidence(signals)
    # Every term key present.
    assert set(comp["terms"].keys()) == {
        "base",
        "extract",
        "crisp",
        "mention",
        "corroboration",
        "conflict",
    }
    assert comp["version"] == EDGE_CONFIDENCE_VERSION
    assert "z" in comp
    # The stored terms sum to z.
    assert isclose(sum(comp["terms"].values()), comp["z"], rel_tol=1e-12)
    # Recompute confidence from the stored z — must match the returned value.
    from afair.substrate.confidence import _clamp_confidence

    assert isclose(c, _clamp_confidence(_sigmoid(comp["z"])), rel_tol=1e-12)


def test_min_over_mentions_ignores_none() -> None:
    # Subject None, object 0.5 → the mention term must use 0.5.
    signals = EdgeConfidenceSignals(
        subject_mention_confidence=None,
        object_mention_confidence=0.5,
        predicate="runs",
    )
    _, comp = compute_edge_confidence(signals)
    from afair.substrate.confidence import W_MENTION

    assert isclose(comp["terms"]["mention"], W_MENTION * (0.5 - 1.0), rel_tol=1e-12)
