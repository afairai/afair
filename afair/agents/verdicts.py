"""Relation verdicts — how one memory relates to another.

Single source of truth for the taxonomy shared by the conflict_resolver
(cold-path pairwise judgments) and the recall caveats layer.

Naming: these are **afair's own** verb-from-the-memory's-perspective names
(``updates``, ``reverts``, ``evolves``, ``conflicts`` …). The *idea* that a
personal-memory system must separate time-updates from genuine errors is sound
(and was sharpened by reading GBrain's judge), but the vocabulary is ours — a
memory layer "shaped by you, not by someone else's template" should not speak
in a competitor's identifiers. ``_ALIASES`` below maps both the original afair
strings (contradicts/compatible/unclear) AND the GBrain-style spellings onto
the current names so any historical row keeps reading correctly (I3).

Why this shape (and why these two beyond a generic conflict enum)
-----------------------------------------------------------------
The naive shape is contradicts/compatible/unclear (what afair shipped first).
It conflates the most common case in a *personal memory* vault with a real
error: most apparent contradictions are time-updates, not conflicts. "Sajinth
is CTO" after "Sajinth is CEO" is the role changing, not a contradiction.

So the time family (``updates`` / ``reverts`` / ``evolves``) carries that
distinction. On top of a generic conflict/compat set afair adds two verdicts a
personal-memory system needs:

- ``confirms`` — independent memories asserting the same thing. afair's value
  is *durable accumulation*, so "N sources agree" is a first-class attestation
  signal (raises confidence; its absence feeds a "thin evidence" caveat).
- ``name_clash`` — same name, different thing. afair's emergent entity graph
  (I6) carries a real same-name-different-referent risk (the entity-dedup
  keep-separate markers exist for exactly this). A verdict that says "these only
  look related because the name matches" prevents a false conflict AND flags a
  possible bad merge.

That is the answer to "do we need more than the time-split + conflict set?":
yes, two — and only two. More would hurt classification reliability without
adding an action.
"""

from __future__ import annotations

from dataclasses import dataclass

# Bump when the verdict set OR the judge prompt changes, so cached judgments
# and stored verdicts are attributable to a prompt generation (frozen-prompt
# discipline, same pattern as JUDGE_PROMPT_VERSION). v2 = afair-native names.
VERDICT_TAXONOMY_VERSION = "v2:2026-06-13"

# A ``conflicts`` verdict is only honoured at or above this confidence; below
# it the judge is treated as not-confident-enough and downgraded to ``unsure``.
# Double-enforced in code (``enforce_confidence_floor``) so a model that
# ignores the prompt floor still cannot raise a low-confidence alarm.
CONFLICT_CONFIDENCE_FLOOR = 0.7


@dataclass(frozen=True)
class VerdictMeta:
    """Actionable metadata so both consumers branch without re-deriving."""

    family: str  # time | conflict | attestation | referent | abstain
    # Should recall WARN the user this hit is in live tension with another?
    unresolved_conflict: bool
    # Is this a "newer memory replaces older" relationship (history vs current)?
    time_update: bool
    # Does this relationship *raise* confidence in the claim?
    raises_confidence: bool
    # Short, user-facing caveat template (None = nothing to surface).
    caveat: str | None


# ── the taxonomy (afair-native names) ───────────────────────────────────────

RELATION_VERDICTS: dict[str, VerdictMeta] = {
    # Time — the newer/older relationship explains the difference.
    "updates": VerdictMeta(
        family="time",
        unresolved_conflict=False,
        time_update=True,
        raises_confidence=False,
        caveat="a newer record updates an older one — the older may be history, not current",
    ),
    "reverts": VerdictMeta(
        family="time",
        unresolved_conflict=True,  # a value moving backwards is worth flagging
        time_update=True,
        raises_confidence=False,
        caveat="a tracked value moved backwards over time — could be a real decline or a mistake",
    ),
    "evolves": VerdictMeta(
        family="time",
        unresolved_conflict=False,
        time_update=False,
        raises_confidence=False,
        caveat=None,  # both true at their times; no warning needed
    ),
    # Conflict — genuine or a false alarm.
    "conflicts": VerdictMeta(
        family="conflict",
        unresolved_conflict=True,
        time_update=False,
        raises_confidence=False,
        caveat="two records genuinely disagree and time does not explain it",
    ),
    "false_conflict": VerdictMeta(
        family="conflict",
        unresolved_conflict=False,  # only looked like a clash (a negation read wrong)
        time_update=False,
        raises_confidence=False,
        caveat=None,
    ),
    # Attestation / compatibility.
    "confirms": VerdictMeta(
        family="attestation",
        unresolved_conflict=False,
        time_update=False,
        raises_confidence=True,
        caveat=None,
    ),
    "unrelated": VerdictMeta(
        family="attestation",
        unresolved_conflict=False,
        time_update=False,
        raises_confidence=False,
        caveat=None,
    ),
    # Referent — afair-specific entity-dedup safety.
    "name_clash": VerdictMeta(
        family="referent",
        unresolved_conflict=False,
        time_update=False,
        raises_confidence=False,
        caveat="these share a name but appear to be different things — a possible mis-merge",
    ),
    # Abstain.
    "unsure": VerdictMeta(
        family="abstain",
        unresolved_conflict=False,
        time_update=False,
        raises_confidence=False,
        caveat=None,
    ),
}

VERDICT_ENUM: list[str] = list(RELATION_VERDICTS)

# Any historical spelling → current name (I3 read-compat). Covers afair's first
# shipped strings AND the GBrain-style names we briefly used before renaming.
_ALIASES = {
    # original afair v0
    "contradicts": "conflicts",
    "compatible": "unrelated",
    "unclear": "unsure",
    # GBrain-style (used for a few hours on 2026-06-13 before the rename)
    "temporal_supersession": "updates",
    "temporal_regression": "reverts",
    "temporal_evolution": "evolves",
    "contradiction": "conflicts",
    "negation_artifact": "false_conflict",
    "corroboration": "confirms",
    "no_relation": "unrelated",
    "different_referent": "name_clash",
    "uncertain": "unsure",
}


def normalize_verdict(verdict: str) -> str:
    """Map any stored verdict (current or historical) onto the current name.

    Unknown strings fall back to ``unsure`` so a future/garbled value never
    crashes a reader and never raises a false alarm.
    """
    if verdict in RELATION_VERDICTS:
        return verdict
    return _ALIASES.get(verdict, "unsure")


def meta(verdict: str) -> VerdictMeta:
    """Metadata for any verdict (normalizes first)."""
    return RELATION_VERDICTS[normalize_verdict(verdict)]


def is_unresolved_conflict(verdict: str) -> bool:
    """True when recall should warn the user about this hit (live tension)."""
    return meta(verdict).unresolved_conflict


def enforce_confidence_floor(verdict: str, confidence: float) -> str:
    """Downgrade a low-confidence ``conflicts`` to ``unsure``.

    Belt-and-suspenders against a model that ignores the prompt-level floor.
    Only ``conflicts`` carries a floor; every other verdict passes through.
    """
    if verdict == "conflicts" and confidence < CONFLICT_CONFIDENCE_FLOOR:
        return "unsure"
    return verdict
