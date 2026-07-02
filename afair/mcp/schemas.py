"""Pydantic schemas for MCP tool inputs and outputs.

These shapes are the v1 forever-contract per Invariant I1.

Three tools, three response shapes:
  - remember  →  RememberResult     (write a fact, optionally invalidate older facts)
  - recall    →  RecallResult       (read: search / by-id / stats — all one verb)
  - observe   →  ObserveResult      (log an action/event)

The 6-tool design (remember/recall/list_context/observe/get_event/invalidate)
was collapsed to 3 during pre-release (2026-05-26, decision event
01KSHW6Q0EB1BBPKZ4Q2QT20NT) before any external user adopted the
surface. After this point, I1 freezes these signatures — new optional
parameters and new response fields are still allowed, but no tool may
be added or removed.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    Field,
    ValidationError,
    WrapValidator,
    model_validator,
)


def _canon_json(obj: Any) -> str:
    """Cheap deterministic serializer for size-bound checks (not for storage)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# 10 MB cap on `remember` content — v1 lock. Raising the cap later is
# additive (smaller clients still work); lowering would break I1.
MAX_REMEMBER_BYTES = 10 * 1024 * 1024
"""Raw-byte ceiling for a single `remember` call's content."""

# Bounded list sizes for kwargs that loop server-side. Bounded string
# lengths for fields that hit FTS5 / embedding chunkers. All caps are
# generous for legitimate use (no real user passes 50+ parent_hashes
# in one call, no real user writes 4KB of context). Adversarial loads
# above these would DOS the validator / FTS index / embedding pipeline.
# Per I1: tightening bounds on a never-documented-unbounded surface is
# additive (no compliant caller depended on infinite-length lists).
MAX_PARENT_HASHES_PER_CALL = 50
MAX_INVALIDATES_PER_CALL = 50
MAX_CONTEXT_CHARS = 4_000
MAX_TYPE_HINT_CHARS = 200
MAX_MIME_CHARS = 200
MAX_FILENAME_HINT_CHARS = 500


# ── remember ────────────────────────────────────────────────────────────────


class TextContent(BaseModel):
    """Text variant of the remember content union.

    NB: ``text`` length isn't capped by Pydantic so the handler can raise
    a typed :class:`ContentTooLargeError` with the actual size in the
    message. Defense-in-depth: the body-size middleware (12 MB raw cap)
    is the first gate; the handler is the second.
    """

    type: Literal["text"]
    text: str


class BinaryContent(BaseModel):
    """Binary variant of the remember content union.

    `data_b64` is base64 of the raw bytes. `mime` is required.
    """

    type: Literal["binary"]
    data_b64: str
    mime: str = Field(min_length=1, max_length=MAX_MIME_CHARS)
    filename_hint: str | None = Field(default=None, max_length=MAX_FILENAME_HINT_CHARS)


class BlobRefContent(BaseModel):
    """Reference to an already-uploaded blob in the object store.

    Used after a streaming-upload via /internal/blob/upload — the
    bytes are already on disk, this just wires an event to them.
    Bypasses the 10 MB JSON-body cap so files up to the deployment
    limit (typically 1 GB) can be remembered without holding the
    blob in RAM.

    ``blob_hash`` MUST be ``sha256:<64-hex>`` — the streaming endpoint
    returns it after the upload completes. If the hash doesn't exist
    in the object store the handler raises ``InvalidateTargetError``
    (similar semantics to a missing invalidates target).
    """

    type: Literal["blob-ref"]
    blob_hash: str = Field(min_length=71, max_length=71)
    mime: str = Field(min_length=1, max_length=MAX_MIME_CHARS)
    filename_hint: str | None = Field(default=None, max_length=MAX_FILENAME_HINT_CHARS)


# ── compound (Tier 3 — atomic multi-payload events) ────────────────────────


MAX_COMPOUND_PARTS = 20
"""Hard cap on parts per compound event. A meeting = transcript +
slides + screenshot is 3; even an aggressive multimodal event
(article + 5 images + 2 audio clips + comments) tops out around 10.
20 is generous and bounds adversarial loads."""


class CompoundTextPart(BaseModel):
    """One text payload inside a compound event."""

    type: Literal["text"]
    text: str
    label: str | None = Field(default=None, max_length=200)
    """Optional human-readable label for the part (e.g. 'transcript',
    'caption'). Stored verbatim; surfaces in recall hits so an AI
    client can address parts by name."""


class CompoundBlobRefPart(BaseModel):
    """One already-uploaded blob inside a compound event."""

    type: Literal["blob-ref"]
    blob_hash: str = Field(min_length=71, max_length=71)
    mime: str = Field(min_length=1, max_length=MAX_MIME_CHARS)
    filename_hint: str | None = Field(default=None, max_length=MAX_FILENAME_HINT_CHARS)
    label: str | None = Field(default=None, max_length=200)


CompoundPart = Annotated[
    CompoundTextPart | CompoundBlobRefPart,
    Field(discriminator="type"),
]


class CompoundContent(BaseModel):
    """Atomic event composed of multiple parts.

    Use when a single semantic memory has more than one representation
    that should travel together: meeting = transcript + slides +
    screenshot; receipt = photo + extracted line items; podcast = mp3
    + transcript + show-notes-markdown.

    Each part is either inline text or a blob-ref to a previously
    streamed-uploaded blob. The compound is stored as ONE event row
    (one content_hash) with the parts array in the payload; recall
    returns it as a single hit with the parts inline.

    Why not just call remember() three times with parent_hashes
    linking? Because atomicity matters: a meeting is one observation,
    not three. parent_hashes is for events that ACTUALLY reference
    earlier events (an update supersedes a previous claim); compound
    is for events that have multiple FACETS.
    """

    type: Literal["compound"]
    parts: list[CompoundPart] = Field(min_length=1, max_length=MAX_COMPOUND_PARTS)


RememberContent = Annotated[
    TextContent | BinaryContent | BlobRefContent | CompoundContent,
    Field(discriminator="type"),
]

_CONTENT_TAGS = {"text", "binary", "blob-ref", "compound"}


def _coerce_remember_content(v: Any, handler: Any) -> Any:
    """Write-first intake: a ``remember`` is never rejected on content shape.

    afair's substrate is append-only and re-interpretable (I2/I3), so the intake
    priority is to accept and persist; the extractor then derives structure from
    the raw event (I6). This normalises the client mistakes we actually see into
    the canonical union instead of raising a ValidationError that would silently
    drop the memory:

    - a bare string -> a text event.
    - a dict whose ``type`` isn't a content tag (e.g. an agent put its
      ``type_hint`` value like 'fact' into ``content.type``) but that carries
      ``text`` -> a text event with that text.
    - anything else that still fails to validate (e.g. ``type: 'binary'`` with no
      data) -> the raw payload serialised as text, so nothing is ever lost.

    A well-formed call passes straight through, so the frozen contract and the
    advertised JSON schema are unchanged (WrapValidator keeps the wrapped
    schema); this only widens what is accepted. `handler` runs the normal
    discriminated-union validation.
    """
    if isinstance(v, str):
        v = {"type": "text", "text": v}
    elif isinstance(v, dict) and v.get("type") not in _CONTENT_TAGS:
        text = v.get("text")
        v = {
            "type": "text",
            "text": text
            if isinstance(text, str)
            else json.dumps(v, ensure_ascii=False, sort_keys=True),
        }
    try:
        return handler(v)
    except ValidationError:
        raw = (
            v
            if isinstance(v, str)
            else json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)
        )
        return handler({"type": "text", "text": raw})


RememberContentInput = Annotated[RememberContent, WrapValidator(_coerce_remember_content)]
"""The `remember` tool parameter type: the canonical union, wrapped in a
write-first coercion so malformed content is normalised (worst case: stored as
text) instead of rejected. The generated JSON schema is still
``RememberContent``'s, so the advertised tool contract (I1) is unchanged."""


class RememberResult(BaseModel):
    """Result of a `remember` call.

    ``invalidated`` carries the content_hashes that were marked superseded
    in this call (via the ``invalidates`` kwarg). Empty list when none
    were invalidated. Invalidation is append-only (I2-conformant) — the
    target events stay in the substrate, the invalidation is a new event
    with ``kind='invalidate'`` referencing them.
    """

    ok: bool
    event_id: str
    content_hash: str
    deduplicated: bool
    invalidated: list[str] = []


# ── recall ──────────────────────────────────────────────────────────────────


Depth = Literal["auto", "shallow", "normal", "deep"]
"""Recall depth selector.

- ``auto``     — system picks based on the query shape (default).
                 Exact identifiers, single words → shallow; otherwise normal.
                 Caller doesn't have to think about it.
- ``shallow``  — FTS5 keyword only. No embedding inference.
- ``normal``   — Hybrid FTS5 + vector via Reciprocal Rank Fusion.
- ``deep``     — Same as normal until the Phase 3+ reasoning agent lands.
"""


class ConflictFlag(BaseModel):
    """One verdict pair from the cold-path Conflict-Resolver (Phase 3).

    Surfaces on a recall hit when a later cycle of the Conflict-Resolver
    judged this event against some other event in the vault. The
    ``verdict`` is one of the relation taxonomy in ``afair/agents/verdicts.py``
    (updates / reverts / evolves / conflicts / false_conflict / confirms /
    unrelated / name_clash / unsure). Historical rows may carry older strings
    (contradicts / compatible / unclear), which normalize on read.
    The AI client uses these to decide whether to surface or suppress
    conflicting facts when answering the user.
    """

    with_event_id: str
    """Event id of the other side of the pair — fetch via ``recall(by_id=...)``."""

    with_content_hash: str

    verdict: str
    """A relation verdict — see afair/agents/verdicts.py VERDICT_ENUM (legacy
    contradicts/compatible/unclear strings still appear on historical rows)."""

    reason: str = ""
    confidence: float = 0.0


class InvalidationSummary(BaseModel):
    """Surfacing of a fact's bi-temporal invalidation status.

    Present when a later event with ``kind='invalidate'`` referenced this
    hit's ``content_hash``. The AI client uses this to decide whether to
    treat the hit as currently-true (``invalidation is None``) or as
    historical context. The original event remains in the substrate
    forever per I2 — this is just the projection.
    """

    at: str
    """ISO 8601 timestamp when the invalidation was recorded (t_invalid)."""

    by_event_id: str
    """Event id of the invalidation — fetch via ``recall(by_id=...)`` for
    full reason + context."""

    reason: str | None = None


class RecallHit(BaseModel):
    """One match returned by `recall`.

    ``payload`` is either the truncated summary view (default — for
    skim-many-results UX) or the full untruncated payload, depending on
    whether the caller passed ``full_payload=True``. ``truncated``
    tells the caller which form they got. For ``content_type ==
    'text-large'`` with ``full_payload=True``, the inline text is read
    back from the object store and surfaced in ``payload.text`` so
    callers see one consistent shape regardless of where bytes lived.

    ``interpretation`` is the latest successful Extractor output —
    best_guess_kind, summary, entities, salient_facts, language,
    confidence, source_attribution. Null when extraction failed or is
    in flight.

    ``linked_event_ids`` are content_hashes the Bind agent automatically
    found to be semantically similar to this hit. Empty list when the
    Bind agent hasn't processed this event yet or found no neighbors.

    ``invalidation`` is non-null when a later event recorded a
    contradiction or supersession for this hit. Recall does NOT filter
    invalidated hits — they surface alongside current ones so the AI
    can decide based on query intent (current state vs history).

    ``conflicts`` lists Conflict-Resolver verdicts touching this event
    (cold-path output). Each entry references the OTHER event in the
    pair plus the verdict + reason.

    ``parent_hashes`` is the lineage list set at write time (empty for
    most events). Populated for events with explicit ``parent_hashes``
    in the original write OR for invalidation events (which carry the
    target's hash there).
    """

    event_id: str
    content_hash: str
    created_at: str
    kind: str
    origin: str
    payload: dict[str, Any]
    truncated: bool
    interpretation: dict[str, Any] | None = None
    linked_event_ids: list[str] = []
    parent_hashes: list[str] = []
    invalidation: InvalidationSummary | None = None
    conflicts: list[ConflictFlag] = []


class ContextSummary(BaseModel):
    """Vault-wide summary populated when ``recall(stats=True)`` is called.

    Standalone counts give the AI a sense of vault size + composition
    without having to enumerate hits. Useful at session start ("what's
    the lay of the land") and for periodic check-ins.
    """

    total_events: int
    by_kind: dict[str, int]
    by_origin: dict[str, int]


class RecallCoverage(BaseModel):
    """Honesty layer over a recall result — "what the vault does NOT (confidently)
    tell you about this query."

    The point is to make recall *honest about its own limits*, so an AI client
    can hedge or ask a follow-up instead of treating thin/stale/contradicted
    memory as settled fact. Computed entirely from signals the hits already
    carry (created_at, conflicts, invalidation, interpretation confidence) — no
    extra LLM call.

    All fields default to the "nothing to flag" value, so an empty/None
    coverage block means "no caveats". ``caveats`` holds the human-readable
    lines an agent can surface verbatim; the structured fields let it branch.

    Additive per Invariant I1 — a new optional field on RecallResult, the three
    frozen verbs are unchanged.
    """

    caveats: list[str] = []
    """Human-readable honesty notes, safe to show the user verbatim."""
    stale_newest_event_days: int | None = None
    """Age in days of the MOST RECENT matching event. Large = even the freshest
    thing the vault knows about this topic is old; it may be out of date."""
    unresolved_contradictions: int = 0
    """Count of returned hits carrying an unresolved conflict verdict."""
    invalidated_hits: int = 0
    """Count of returned hits a later event superseded/contradicted."""
    thin_evidence: bool = False
    """True when the query matched very little — the vault likely doesn't hold
    this yet."""
    low_confidence_edges: int = 0
    """Count of served, unreviewed relations in these results below the
    low-confidence caveat threshold (ADR-0004). Non-zero means the vault is
    surfacing tentative beliefs alongside the memories — treat them as guesses,
    not settled fact. The edges are still shown (recall honesty), and each is
    reviewable through the recall(decide=...) loop."""


class ProposedCorrectionView(BaseModel):
    """One open proposal awaiting the operator's decision — an entity-audit
    correction OR a Schema-Evolver ontology revision (ADR-0003 Phase 5).

    Surfaced on ``recall(stats=True)`` (the session-start / check-in call) so
    the AI client can raise it conversationally and, on a yes, confirm it via
    ``recall(decide=...)``. ``prompt`` is a ready-to-ask yes/no question; the
    structured fields let the client explain or branch.

    Additive per Invariant I1 — a new optional field on RecallResult; the three
    frozen verbs keep their signatures. Ontology proposals reuse the same view
    (same list, same decide loop): their ``kind`` is ``'ontology_<action>'``,
    the entity fields stay empty, and ``subject_slug`` names the kind the
    revision touches.
    """

    id: str
    """Pass back as ``CorrectionDecision.proposal_id`` to confirm/reject."""
    kind: str
    """'retype' | 'merge' | 'merge_review' for entity proposals;
    'ontology_add' | 'ontology_rename' | 'ontology_merge' | 'ontology_split'
    | 'ontology_deprecate' for ontology proposals."""
    entity_id: str = ""
    entity_name: str = ""
    prompt: str
    """Human-readable yes/no question, safe to show the user verbatim."""
    evidence: str
    """Why the audit flagged it — the pattern that fired."""
    confidence: float
    subject_slug: str | None = None
    """Ontology proposals only: the kind slug the revision is about (for
    'ontology_add', the PROPOSED new slug)."""


class RecallResult(BaseModel):
    """Result of any `recall` call.

    Six call modes share this shape:
      - ``recall(query=...)``                      → search via FTS+vector
      - ``recall(by_id=...)``                      → single-event lookup
      - ``recall(by_content_hash=...)``            → single-event lookup
      - ``recall(stats=True)``                     → summary + recent hits
      - ``recall(query=..., full_payload=True)``   → search, untruncated
      - ``recall()``                               → most-recent N hits

    ``summary`` is only populated when ``stats=True`` was requested.
    ``coverage`` is the honesty layer (see RecallCoverage) — populated on
    query/browse results, null on single-event lookups.
    ``pending_corrections`` lists open entity-audit AND ontology proposals —
    populated on ``stats=True`` (and on any call that carried a ``decide``,
    so the client sees the remaining queue after acting).
    ``pending_corrections_count`` is the TRUE total of open proposals
    (entity-audit + ontology) and is populated on EVERY call — a cheap
    nudge signal; call ``stats=True`` to fetch the list itself.
    """

    hits: list[RecallHit]
    depth_used: Depth
    note: str | None = None
    summary: ContextSummary | None = None
    coverage: RecallCoverage | None = None
    pending_corrections: list[ProposedCorrectionView] = []
    pending_corrections_count: int = 0


# ── recall feedback ─────────────────────────────────────────────────────────


MAX_FEEDBACK_IDS_PER_CALL = 50
MAX_FEEDBACK_TOPIC_CHARS = 500


class RecallFeedback(BaseModel):
    """Optional caller-supplied signal on PRIOR recall hits.

    The MCP-client AI calls ``recall(...)`` once to get hits, then on
    its NEXT recall passes a ``feedback`` payload referring to those
    earlier hits. The signal drives the self-improvement tuner — see
    the recursive self-improvement design.

    All fields optional. Empty payload is a no-op. IDs over the cap
    are truncated silently to keep one inflated client from flooding
    the substrate.

    Why optional + on the existing tool: I1 forbids new tools.
    Additive optional args on an existing tool are allowed (shipped
    signatures keep working for clients that don't send feedback).
    """

    useful_event_ids: list[str] = []
    """Event IDs from a prior recall that the caller found helpful."""

    not_useful_event_ids: list[str] = []
    """Event IDs from a prior recall that the caller found off-target."""

    missing_topic: str | None = None
    """Free-text note about what the prior recall did NOT surface
    that the caller expected. Capped at MAX_FEEDBACK_TOPIC_CHARS."""


# ── correction decision ──────────────────────────────────────────────────────


class CorrectionDecision(BaseModel):
    """The operator's verdict on one pending proposal — an entity-audit
    correction or a Schema-Evolver ontology revision.

    The client first sees proposals via ``recall(stats=True).pending_corrections``,
    asks the user, then passes the decision back on its next recall:
    ``recall(decide=CorrectionDecision(proposal_id=..., verdict="confirm"))``.
    A confirm applies the change through the append-only primitives and
    records it; a reject closes the proposal untouched. Deciding an
    already-decided proposal is a no-op. Ids carrying the ``ont_`` prefix
    route to the ontology queue (ADR-0003 Phase 5) — same argument, same
    loop, no new tool.

    Why optional + on the existing tool: I1 forbids new tools. Additive optional
    args are allowed, exactly like ``feedback``.
    """

    proposal_id: str
    """``ProposedCorrectionView.id`` from a prior recall."""
    verdict: Literal["confirm", "reject", "retract", "revert"]
    """``confirm`` keeps the proposed/auto state; ``reject`` corrects it;
    ``retract`` withdraws the entity entirely (it's noise — a file path, a test
    fixture — not a real entity; entity proposals only); ``revert`` undoes a
    previously APPLIED ontology revision by appending the compensating
    revision (I7; ontology proposals only)."""
    to_kind: str | None = None
    """For a ``merge_review`` reject: the corrected entity kind ("no, Clario is
    a project, not a product"). The assisting AI maps the user's natural-language
    answer to one of the known kinds. Ignored for other proposal kinds."""


# ── observe ─────────────────────────────────────────────────────────────────


MAX_OBSERVE_ACTION_CHARS = 200
MAX_OBSERVE_SUBJECT_CHARS = 1_000
MAX_OBSERVE_RESULT_CHARS = 2_000
MAX_OBSERVE_EXTRAS_BYTES = 64 * 1024

_OBSERVE_FIELD_CAPS = {
    "action": MAX_OBSERVE_ACTION_CHARS,
    "subject": MAX_OBSERVE_SUBJECT_CHARS,
    "result": MAX_OBSERVE_RESULT_CHARS,
}
"""Per-field character caps for the write-first truncation in
``ObserveEvent._accept_first``. Over-long values are truncated to the cap
and the full original preserved under ``<field>_full`` rather than rejected."""
"""Caps for observe() inputs. ``extras`` is the free-form open dict; the
size cap and nesting check below stop deeply-nested JSON bombs and
extras floods that would inflate FTS index / SQLite payload row."""


class ObserveEvent(BaseModel):
    """An agent-self-logged event. ``action`` is required; other keys are
    recognized or preserved verbatim.

    Configured to allow arbitrary additional fields so different AI clients
    can use whatever shape fits their mental model. The extras are size-
    and nesting-bounded — see ``_bound_extras``.
    """

    model_config = {"extra": "allow"}

    action: str = Field(min_length=1, max_length=MAX_OBSERVE_ACTION_CHARS)
    subject: str | None = Field(default=None, max_length=MAX_OBSERVE_SUBJECT_CHARS)
    result: str | None = Field(default=None, max_length=MAX_OBSERVE_RESULT_CHARS)

    @model_validator(mode="before")
    @classmethod
    def _accept_first(cls, data: Any) -> Any:
        """Write-first intake: an ``observe`` is never rejected for a missing
        action or for an over-long field. ``action`` is the only hard
        requirement, so we default it rather than drop the event (same
        principle as remember's content coercion).

        - a dict without a usable ``action`` -> action defaults to 'observed'.
        - a bare string -> that string becomes the action.
        - anything else -> kept under a default action so it is still logged.

        Over-long ``action`` / ``subject`` / ``result`` are truncated to their
        caps so the Field length constraints never reject a real payload; the
        full original is preserved verbatim under ``<field>_full`` (I2 spirit —
        nothing the caller sent is lost). A live client that stuffs a whole
        JSON blob into ``action`` therefore persists rather than being dropped
        at the signature layer.
        """
        if isinstance(data, dict):
            coerced = dict(data)
            action = coerced.get("action")
            if not isinstance(action, str) or not action.strip():
                coerced["action"] = "observed"
            return cls._truncate_long_fields(coerced)
        if isinstance(data, str):
            return cls._truncate_long_fields({"action": data.strip() or "observed"})
        dump = json.dumps(data, ensure_ascii=False, default=str)
        return cls._truncate_long_fields({"action": "observed", "result": dump})

    @staticmethod
    def _truncate_long_fields(data: dict[str, Any]) -> dict[str, Any]:
        """Truncate over-long ``action`` / ``subject`` / ``result`` to their
        caps, preserving the full original under ``<field>_full`` so the
        Field constraints never fire on real input and no data is lost."""
        for field, cap in _OBSERVE_FIELD_CAPS.items():
            value = data.get(field)
            if isinstance(value, str) and len(value) > cap:
                full_key = f"{field}_full"
                # Don't clobber a caller-supplied ``<field>_full``: keep theirs
                # under ``<field>_full_client`` so nothing the caller sent is lost.
                if full_key in data and data[full_key] != value:
                    data[f"{full_key}_client"] = data[full_key]
                data[full_key] = value
                data[field] = value[:cap]
        return data

    @model_validator(mode="after")
    def _bound_extras(self) -> ObserveEvent:
        """Cap the size + nesting of the free-form extras dict.

        Pydantic stores extras (the keys beyond action/subject/result)
        in ``__pydantic_extra__``. Without a cap an adversarial client
        could send a 12MB nested-bomb payload that DOSes the FTS index
        and inflates every recall hit's row deserialization cost.
        """
        extras = self.__pydantic_extra__
        if not extras:
            return self
        serialized = _canon_json(extras)
        if len(serialized) > MAX_OBSERVE_EXTRAS_BYTES:
            msg = f"observe extras must be <= {MAX_OBSERVE_EXTRAS_BYTES} bytes serialized"
            raise ValueError(msg)
        # Cheap nesting check — too many braces/brackets means the dict
        # is either huge or arbitrarily nested. Block before the recursion
        # hits Python's RecursionError on deserialize.
        if serialized.count("{") + serialized.count("[") > 200:
            msg = "observe extras exceed nesting threshold (200 containers)"
            raise ValueError(msg)
        return self


class ObserveResult(BaseModel):
    ok: bool
    event_id: str
    content_hash: str
