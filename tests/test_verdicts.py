"""Relation-verdict taxonomy — the shared conflict/relation vocabulary.

Guards the taxonomy afair adapted from GBrain's 6-verdict judge (temporal
family) plus afair's two additions (corroboration, different_referent), the
legacy normalization (I3 read-compat), and the contradiction confidence floor.
"""

from __future__ import annotations

import pytest

from afair.agents.verdicts import (
    CONFLICT_CONFIDENCE_FLOOR,
    RELATION_VERDICTS,
    VERDICT_ENUM,
    enforce_confidence_floor,
    is_unresolved_conflict,
    meta,
    normalize_verdict,
)


def test_taxonomy_has_the_nine_expected_verdicts() -> None:
    assert set(VERDICT_ENUM) == {
        "updates",
        "reverts",
        "evolves",
        "conflicts",
        "false_conflict",
        "confirms",
        "unrelated",
        "name_clash",
        "unsure",
    }
    assert len(RELATION_VERDICTS) == 9


def test_historical_verdicts_normalize_onto_current_names() -> None:
    # Original afair v0 strings.
    assert normalize_verdict("contradicts") == "conflicts"
    assert normalize_verdict("compatible") == "unrelated"
    assert normalize_verdict("unclear") == "unsure"
    # GBrain-style names briefly used before the rename normalize too.
    assert normalize_verdict("temporal_supersession") == "updates"
    assert normalize_verdict("different_referent") == "name_clash"
    assert normalize_verdict("contradiction") == "conflicts"
    # Current values pass through unchanged.
    assert normalize_verdict("updates") == "updates"
    # Unknown/garbled → safe abstain, never a false alarm.
    assert normalize_verdict("garbage_value") == "unsure"


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        # Canonical names observed in live vaults pass through unchanged.
        ("conflicts", "conflicts"),
        ("name_clash", "name_clash"),
        ("confirms", "confirms"),
        ("evolves", "evolves"),
        ("updates", "updates"),
        ("unrelated", "unrelated"),
        ("reverts", "reverts"),
        ("unsure", "unsure"),
        # Historical spellings observed in live vaults (rows written before
        # the v2:2026-06-13 rename) resolve via the alias map.
        ("contradicts", "conflicts"),
        ("compatible", "unrelated"),
        ("corroboration", "confirms"),
        ("temporal_evolution", "evolves"),
        ("no_relation", "unrelated"),
    ],
)
def test_every_vault_observed_label_normalizes(stored: str, expected: str) -> None:
    """Pin every verdict spelling seen in real vault data to the taxonomy.

    A 2026-07 vault checkup found exactly these 13 distinct labels across
    2,015 conflict-resolver runs. Historical rows stay as written (I2/I3);
    readers depend on this mapping, so dropping an alias would silently
    degrade old rows to 'unsure'.
    """
    assert normalize_verdict(stored) == expected


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ("conflicts", True),
        ("reverts", True),  # backwards-moving value is worth flagging
        ("updates", False),  # a normal update, not a conflict
        ("evolves", False),
        ("false_conflict", False),
        ("confirms", False),
        ("unrelated", False),
        ("name_clash", False),
        ("unsure", False),
        ("contradicts", True),  # historical string still resolves correctly
    ],
)
def test_unresolved_conflict_classification(verdict: str, expected: bool) -> None:
    assert is_unresolved_conflict(verdict) is expected


def test_confidence_floor_downgrades_low_confidence_conflict() -> None:
    below = CONFLICT_CONFIDENCE_FLOOR - 0.01
    at = CONFLICT_CONFIDENCE_FLOOR
    assert enforce_confidence_floor("conflicts", below) == "unsure"
    assert enforce_confidence_floor("conflicts", at) == "conflicts"
    # The floor applies only to conflicts; nothing else is touched.
    assert enforce_confidence_floor("reverts", 0.1) == "reverts"
    assert enforce_confidence_floor("confirms", 0.1) == "confirms"


def test_confirms_raises_confidence_and_carries_no_caveat() -> None:
    m = meta("confirms")
    assert m.raises_confidence is True
    assert m.caveat is None


def test_afair_specific_verdicts_have_user_facing_caveats() -> None:
    # The afair additions and the time/conflict flags carry a caveat the recall
    # layer can surface verbatim.
    assert meta("name_clash").caveat is not None
    assert meta("updates").caveat is not None
    assert meta("conflicts").caveat is not None
