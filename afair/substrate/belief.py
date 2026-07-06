"""Belief revision for the derived layer (ADR-0002).

The entity graph is a set of *defeasible beliefs* over the immutable substrate,
not silent truth. This module is the pure logic of that model:

- **Entrenchment** — an AGM-style total order on how hard a belief is to
  retract, derived from its provenance. On conflict the least-entrenched
  belief yields, and a belief is never more entrenched than its
  least-entrenched justification.
- **Auto-confirm policy** — whether a freshly-derived edge may be trusted
  without review, or must wait in the quarantine queue. Only the uncertain are
  queued, so review effort stays small.
- **Trust state** — what an edge's current standing is, resolved from the
  append-only signals (the operator's reviews + cascade invalidations).

No DB and no I/O: callers pass the rows in. See ADR-0002 for the grounding
(AGM belief revision, justification-based TMS, KG human-in-the-loop curation).
"""

from __future__ import annotations

from enum import IntEnum, StrEnum


class Entrenchment(IntEnum):
    """How hard a belief is to retract — higher wins a conflict.

    A fact's entrenchment is bounded by the entrenchment of its
    least-entrenched justification: an edge grounded in a foreign-imported
    record is itself foreign-grade, however confident the synthesis looked.
    """

    FOREIGN_IMPORT = 0
    """Migrated from another AI's memory. Lowest — never auto-trusted; a
    memory import is a security boundary (cf. memory-injection attacks), not
    just a quality one."""

    AGENT_DERIVED = 1
    """Synthesized by the cold path (the default for an entity edge)."""

    USER_STATED = 2
    """Asserted directly by the operator in a remember/observe event."""

    USER_CONFIRMED = 3
    """The operator reviewed it and confirmed. Highest."""


class TrustState(StrEnum):
    """An edge's current standing, surfaced to recall so a proposed belief is
    never served as hard fact."""

    CONFIRMED = "confirmed"
    AUTO_CONFIRMED = "auto_confirmed"
    PROPOSED = "proposed"
    REJECTED = "rejected"


# Auto-confirm thresholds. A derived edge skips the review queue only when it
# clears all of these; otherwise it is `proposed`.
_MIN_AUTO_CONFIRM_CONFIDENCE = 0.75
_MAX_PREDICATE_WORDS = 4
"""A real relation is a short verb-phrase ("runs", "is design partner for").
Confabulated profile-language is long and vague ("is tech person in circle
of", "shares Business/Product role with"). Word count is a cheap, effective
proxy for that tell — see the false edges that prompted ADR-0002."""


def predicate_is_crisp(predicate: str) -> bool:
    """True for a short, relation-shaped predicate; False for vague
    profile-language. The single most discriminating signal between a real
    edge and a co-occurrence confabulation."""
    words = predicate.split()
    return 1 <= len(words) <= _MAX_PREDICATE_WORDS


# Predicate stems that describe a TEXTUAL / stative / comparative relationship
# rather than a durable fact about the world. "X mentions Y", "X is similar to
# Y", "X awaits Y" are true of a passage, not a lasting property of the entities
# — persisting them as graph edges fills the review queue with noise. Matched as
# whole-normalized-predicate or a stem-prefixed phrase after stripping leading
# auxiliaries; see :func:`predicate_is_durable`.
_NON_DURABLE_PREDICATE_STEMS: frozenset[str] = frozenset(
    {
        "await",
        "awaits",
        "awaiting",
        "contrast",
        "contrasts",
        "differ",
        "differs",
        "compare",
        "compares",
        "compared",
        "resemble",
        "resembles",
        "similar to",
        "relate",
        "relates",
        "related to",
        "associated with",
        "linked to",
        "connected to",
        "mention",
        "mentions",
        "mentioned",
        "reference",
        "references",
        "referenced",
        "refers to",
        "discuss",
        "discusses",
        "discussed",
        "criticize",
        "criticizes",
        "criticized",
        "retain",
        "retains",
        "involve",
        "involves",
        "involved in",
        "regarding",
        "about",
        "seems",
        "appears",
        "may be",
        "might be",
    }
)

# Leading auxiliary/copular verbs stripped before stem-matching so "is similar
# to", "was awaiting", "has referenced" all reduce to their stem. Order-free:
# stripped repeatedly from the front.
_AUX_PREFIXES: tuple[str, ...] = (
    "is ",
    "was ",
    "are ",
    "were ",
    "has ",
    "have ",
    "had ",
    "been ",
    "being ",
    "will ",
    "would ",
)


def predicate_is_durable(predicate: str) -> bool:
    """False for a stative / textual / comparative predicate that shouldn't be
    persisted as a graph edge; True otherwise.

    A relation-QUALITY gate, NOT an entity ontology (I6): it decides whether a
    triple is worth keeping as a durable belief, never what KIND an entity is.
    Fail-OPEN by design — an unclassifiable predicate is treated as durable
    (this is a noise filter, not a truth oracle, so when in doubt keep the
    edge). Pure; no I/O.

    The predicate is lowercased and whitespace-collapsed, leading auxiliaries
    stripped, then judged non-durable iff the result IS a known non-durable stem
    or starts with ``stem + " "``. ADR-0002's positive examples ("runs", "is
    design partner for", "works at", "owns", "married to") all stay durable.
    """
    norm = " ".join(predicate.lower().split())
    if not norm:
        return True  # fail-open: nothing to judge
    # Strip leading auxiliaries repeatedly ("is being" → "").
    changed = True
    while changed:
        changed = False
        for aux in _AUX_PREFIXES:
            if norm.startswith(aux):
                norm = norm[len(aux) :]
                changed = True
                break
    if not norm:
        return True  # a bare auxiliary is not a claim we can gate — keep it
    for stem in _NON_DURABLE_PREDICATE_STEMS:
        if norm == stem or norm.startswith(stem + " "):
            return False
    return True


def auto_confirm(
    *,
    confidence: float,
    predicate: str,
    source_entrenchment: Entrenchment,
    has_evidence: bool = True,
    floor: float = _MIN_AUTO_CONFIRM_CONFIDENCE,
) -> bool:
    """Whether a freshly-derived edge may be trusted without operator review.

    Trusts only edges that are evidence-grounded, crisply-predicated, above the
    confidence ``floor``, and NOT grounded in a foreign import. Everything else
    is `proposed` and goes to the queue — queuing only the uncertain keeps
    review effort small (ADR-0002 §4).

    ``floor`` defaults to the static ``_MIN_AUTO_CONFIRM_CONFIDENCE`` so every
    existing caller is byte-compatible; recall now passes the SERVED confidence
    (ADR-0004) and the tuner-resolved floor, which finally makes this gate
    discriminate (before ADR-0004 every edge was a flat 0.8 that always cleared
    the floor).
    """
    if source_entrenchment <= Entrenchment.FOREIGN_IMPORT:
        return False
    if not has_evidence:
        return False
    if confidence < floor:
        return False
    return predicate_is_crisp(predicate)


def resolve_trust(
    *,
    latest_verdict: str | None,
    is_invalidated: bool,
    auto_confirmed: bool,
) -> TrustState:
    """An edge's current trust state from the append-only signals.

    The **invalidation row is the canonical reject signal** — it is what the
    graph reads (``iter_edges_for_entity`` etc.) key off, and a ``reject``
    verdict always writes one. Keying off it here keeps the displayed trust
    state and the served graph in agreement, and it is the defeasibly-correct
    behaviour: a defeater wins. A cascade invalidation (the operator corrected
    the source) therefore retracts an edge even over a prior ``confirm`` — if
    the justification is defeated, the conclusion falls.

    Precedence: invalidated → ``rejected``; else an explicit ``confirm`` →
    ``confirmed``; else the auto-confirm policy → ``auto_confirmed`` |
    ``proposed``.

    Known limitation (future slice): re-confirming an already-invalidated edge
    is not supported — the invalidation persists (append-only), so the edge
    stays rejected. Re-asserting a retracted belief is a fresh edge, not an
    un-reject.
    """
    if is_invalidated:
        return TrustState.REJECTED
    if latest_verdict == "confirm":
        return TrustState.CONFIRMED
    return TrustState.AUTO_CONFIRMED if auto_confirmed else TrustState.PROPOSED
