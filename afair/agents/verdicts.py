"""Relation verdicts — how two events about the same thing relate.

Single source of truth for the taxonomy shared by the conflict_resolver
(cold-path pairwise judgments) and the recall caveats layer.

Why this taxonomy is shaped the way it is
------------------------------------------
The naive shape is contradicts/compatible/unclear (what afair shipped first).
That conflates the single most common case in a *personal memory* vault with a
real error: most apparent contradictions are **time-updates**, not conflicts.
"Sajinth is CTO" after "Sajinth is CEO" is not a contradiction — it is the role
changing. Treating it as a conflict is the core failure mode.

So the temporal family (supersession / regression / evolution) is borrowed in
spirit from GBrain's 6-verdict judge (MIT; idea only, reimplemented here). On
top of GBrain's set we add **two verdicts a personal-memory system needs that a
VC/CRM brain does not**:

- ``corroboration`` — afair's whole value is *durable accumulated* memory, so
  "N independent events assert the same thing" is a first-class attestation
  signal (raises confidence, and its absence feeds a "thin evidence" caveat).
  GBrain does not model this.
- ``different_referent`` — afair's emergent entity graph (I6) carries a real
  same-name-different-person risk (the entity-dedup keep-separate markers exist
  for exactly this). A verdict that says "these only look related because the
  surface form matches, but they are different referents" both prevents a false
  conflict AND flags a possible bad merge. A curated VC/CRM brain does not face
  this; an emergent one does.

That is the answer to "do we need more than 6?": yes, two — and only two. More
would hurt classification reliability without adding an action.

Backward compatibility (I3): historical substrate rows carry the legacy
strings (contradicts/compatible/unclear). They stay readable forever;
``normalize_verdict`` maps them onto the current taxonomy on read.
"""

from __future__ import annotations

from dataclasses import dataclass

# Bump when the verdict set OR the judge prompt changes, so cached judgments
# and stored verdicts are attributable to a prompt generation (frozen-prompt
# discipline, same pattern as JUDGE_PROMPT_VERSION).
VERDICT_TAXONOMY_VERSION = "v1:2026-06-13"

# A `contradiction` verdict is only honoured at or above this confidence; below
# it the judge is treated as not-confident-enough and the verdict is downgraded
# to ``uncertain``. Double-enforced in code (``enforce_confidence_floor``) so a
# model that ignores the prompt floor still cannot raise a low-confidence alarm.
CONTRADICTION_CONFIDENCE_FLOOR = 0.7


@dataclass(frozen=True)
class VerdictMeta:
    """Actionable metadata so both consumers branch without re-deriving."""

    family: str  # temporal | conflict | attestation | referent | abstain
    # Should recall WARN the user this hit is in live tension with another?
    unresolved_conflict: bool
    # Is this a "newer fact replaces older" relationship (history vs current)?
    temporal_update: bool
    # Does this relationship *raise* confidence in the claim?
    raises_confidence: bool
    # Short, user-facing caveat template (None = nothing to surface).
    caveat: str | None


# ── the taxonomy ────────────────────────────────────────────────────────────

RELATION_VERDICTS: dict[str, VerdictMeta] = {
    # Temporal — time explains the difference (the core insight).
    "temporal_supersession": VerdictMeta(
        family="temporal",
        unresolved_conflict=False,
        temporal_update=True,
        raises_confidence=False,
        caveat="a newer record supersedes an older one — the older may be history, not current",
    ),
    "temporal_regression": VerdictMeta(
        family="temporal",
        unresolved_conflict=True,  # worth flagging — real decline OR an error
        temporal_update=True,
        raises_confidence=False,
        caveat="a tracked value moved backwards over time — could be a real decline or a mistake",
    ),
    "temporal_evolution": VerdictMeta(
        family="temporal",
        unresolved_conflict=False,
        temporal_update=False,
        raises_confidence=False,
        caveat=None,  # both true at their timestamps; no warning needed
    ),
    # Conflict — genuine or an artifact.
    "contradiction": VerdictMeta(
        family="conflict",
        unresolved_conflict=True,
        temporal_update=False,
        raises_confidence=False,
        caveat="two records genuinely disagree and time does not explain it",
    ),
    "negation_artifact": VerdictMeta(
        family="conflict",
        unresolved_conflict=False,  # apparent conflict is a parse artifact
        temporal_update=False,
        raises_confidence=False,
        caveat=None,
    ),
    # Attestation / compatibility.
    "corroboration": VerdictMeta(
        family="attestation",
        unresolved_conflict=False,
        temporal_update=False,
        raises_confidence=True,
        caveat=None,
    ),
    "no_relation": VerdictMeta(
        family="attestation",
        unresolved_conflict=False,
        temporal_update=False,
        raises_confidence=False,
        caveat=None,
    ),
    # Referent — afair-specific entity-dedup safety.
    "different_referent": VerdictMeta(
        family="referent",
        unresolved_conflict=False,
        temporal_update=False,
        raises_confidence=False,
        caveat="these share a name but appear to be different things — a possible mis-merge",
    ),
    # Abstain.
    "uncertain": VerdictMeta(
        family="abstain",
        unresolved_conflict=False,
        temporal_update=False,
        raises_confidence=False,
        caveat=None,
    ),
}

VERDICT_ENUM: list[str] = list(RELATION_VERDICTS)

# Legacy → current, for reading historical substrate rows (I3).
_LEGACY_MAP = {
    "contradicts": "contradiction",
    "compatible": "no_relation",
    "unclear": "uncertain",
}


def normalize_verdict(verdict: str) -> str:
    """Map any stored verdict (legacy or current) onto the current taxonomy.

    Unknown strings fall back to ``uncertain`` so a future/garbled value never
    crashes a reader and never raises a false alarm.
    """
    if verdict in RELATION_VERDICTS:
        return verdict
    return _LEGACY_MAP.get(verdict, "uncertain")


def meta(verdict: str) -> VerdictMeta:
    """Metadata for any verdict (normalizes first)."""
    return RELATION_VERDICTS[normalize_verdict(verdict)]


def is_unresolved_conflict(verdict: str) -> bool:
    """True when recall should warn the user about this hit (live tension)."""
    return meta(verdict).unresolved_conflict


def enforce_confidence_floor(verdict: str, confidence: float) -> str:
    """Downgrade a low-confidence ``contradiction`` to ``uncertain``.

    Belt-and-suspenders against a model that ignores the prompt-level floor.
    Only ``contradiction`` carries a floor; every other verdict passes through.
    """
    if verdict == "contradiction" and confidence < CONTRADICTION_CONFIDENCE_FLOOR:
        return "uncertain"
    return verdict
