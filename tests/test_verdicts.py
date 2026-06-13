"""Relation-verdict taxonomy — the shared conflict/relation vocabulary.

Guards the taxonomy afair adapted from GBrain's 6-verdict judge (temporal
family) plus afair's two additions (corroboration, different_referent), the
legacy normalization (I3 read-compat), and the contradiction confidence floor.
"""

from __future__ import annotations

import pytest

from afair.agents.verdicts import (
    CONTRADICTION_CONFIDENCE_FLOOR,
    RELATION_VERDICTS,
    VERDICT_ENUM,
    enforce_confidence_floor,
    is_unresolved_conflict,
    meta,
    normalize_verdict,
)


def test_taxonomy_has_the_nine_expected_verdicts() -> None:
    assert set(VERDICT_ENUM) == {
        "temporal_supersession",
        "temporal_regression",
        "temporal_evolution",
        "contradiction",
        "negation_artifact",
        "corroboration",
        "no_relation",
        "different_referent",
        "uncertain",
    }
    assert len(RELATION_VERDICTS) == 9


def test_legacy_verdicts_normalize_onto_current_taxonomy() -> None:
    assert normalize_verdict("contradicts") == "contradiction"
    assert normalize_verdict("compatible") == "no_relation"
    assert normalize_verdict("unclear") == "uncertain"
    # Current values pass through unchanged.
    assert normalize_verdict("temporal_supersession") == "temporal_supersession"
    # Unknown/garbled → safe abstain, never a false alarm.
    assert normalize_verdict("garbage_value") == "uncertain"


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ("contradiction", True),
        ("temporal_regression", True),  # backwards-moving value is worth flagging
        ("temporal_supersession", False),  # a normal update, not a conflict
        ("temporal_evolution", False),
        ("negation_artifact", False),
        ("corroboration", False),
        ("no_relation", False),
        ("different_referent", False),
        ("uncertain", False),
        ("contradicts", True),  # legacy string still resolves correctly
    ],
)
def test_unresolved_conflict_classification(verdict: str, expected: bool) -> None:
    assert is_unresolved_conflict(verdict) is expected


def test_confidence_floor_downgrades_low_confidence_contradiction() -> None:
    below = CONTRADICTION_CONFIDENCE_FLOOR - 0.01
    at = CONTRADICTION_CONFIDENCE_FLOOR
    assert enforce_confidence_floor("contradiction", below) == "uncertain"
    assert enforce_confidence_floor("contradiction", at) == "contradiction"
    # The floor applies only to contradiction; nothing else is touched.
    assert enforce_confidence_floor("temporal_regression", 0.1) == "temporal_regression"
    assert enforce_confidence_floor("corroboration", 0.1) == "corroboration"


def test_corroboration_raises_confidence_and_carries_no_caveat() -> None:
    m = meta("corroboration")
    assert m.raises_confidence is True
    assert m.caveat is None


def test_afair_specific_verdicts_have_user_facing_caveats() -> None:
    # The two afair additions and the temporal/conflict flags carry a caveat
    # the recall layer can surface verbatim.
    assert meta("different_referent").caveat is not None
    assert meta("temporal_supersession").caveat is not None
    assert meta("contradiction").caveat is not None
