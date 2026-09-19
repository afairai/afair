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
    from afair.substrate.confidence import W_CRISP, W_VAGUE

    assert isclose(c_crisp, _sigmoid(_logit(DEFAULT_BASE_RATE) + W_CRISP), rel_tol=1e-12)

    # Vague predicate, nothing else: z == logit(base) - W_VAGUE exactly (the
    # crisp/vague term is asymmetric: small bonus, larger penalty).
    vague = EdgeConfidenceSignals(predicate="is a person in the circle of")
    c_vague, _ = compute_edge_confidence(vague)
    assert isclose(c_vague, _sigmoid(_logit(DEFAULT_BASE_RATE) - W_VAGUE), rel_tol=1e-12)


def test_vague_uncorroborated_stays_below_expiry_floor() -> None:
    """The W_VAGUE sizing guarantee: a vague predicate with ZERO corroboration
    stays below 0.5 at the default base rate, even in the worst case (a perfect
    extraction self-assessment and exact mentions — every other term is <= 0).
    So an uncorroborated vague derivation silently expires instead of reaching
    the operator's review queue (2026-08-10 operator decision)."""
    worst = EdgeConfidenceSignals(
        extraction_confidence=1.0,
        subject_mention_confidence=1.0,
        object_mention_confidence=1.0,
        predicate="is a person in the circle of",
        corroborating_sources=0,
    )
    c, _ = compute_edge_confidence(worst)
    assert c < 0.5

    # Typical vague derivation (the 2026-08-10 example shape).
    typical = EdgeConfidenceSignals(
        extraction_confidence=0.8,
        subject_mention_confidence=1.0,
        object_mention_confidence=0.9,
        predicate="wants to install components on",
        corroborating_sources=0,
    )
    c_typ, _ = compute_edge_confidence(typical)
    assert c_typ < 0.5


def test_intent_predicate_counts_as_vague() -> None:
    """Intent/hedge language is vague even when short enough to pass the
    word-count check: "wants to install" (3 words) scores like a vague
    predicate, not like a crisp relation."""
    intent = EdgeConfidenceSignals(predicate="wants to install")
    crisp = EdgeConfidenceSignals(predicate="runs")
    c_intent, comp = compute_edge_confidence(intent)
    c_crisp, _ = compute_edge_confidence(crisp)
    assert c_intent < c_crisp
    assert comp["terms"]["crisp"] < 0  # penalized, not bonused


def test_corroboration_lifts_vague_back_over_the_floor() -> None:
    """The escape hatch: corroborating sources can lift a vague derivation back
    over 0.5 — vague claims earn attention through corroboration, never by
    default."""
    vague = EdgeConfidenceSignals(predicate="is a person in the circle of")
    c0, _ = compute_edge_confidence(vague)
    c1, _ = compute_edge_confidence(vague.model_copy(update={"corroborating_sources": 1}))
    assert c0 < 0.5
    assert c1 > c0
    assert c1 >= 0.5


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
