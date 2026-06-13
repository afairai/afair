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

from pydantic import BaseModel, Field, field_validator, model_validator


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
    ``verdict`` is the LLM's call: ``contradicts`` / ``compatible`` /
    ``unclear``. The AI client uses these to decide whether to surface
    or suppress conflicting facts when answering the user.
    """

    with_event_id: str
    """Event id of the other side of the pair — fetch via ``recall(by_id=...)``."""

    with_content_hash: str

    verdict: str
    """One of: contradicts, compatible, unclear."""

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
    """

    hits: list[RecallHit]
    depth_used: Depth
    note: str | None = None
    summary: ContextSummary | None = None
    coverage: RecallCoverage | None = None


# ── recall feedback ─────────────────────────────────────────────────────────


MAX_FEEDBACK_IDS_PER_CALL = 50
MAX_FEEDBACK_TOPIC_CHARS = 500


class RecallFeedback(BaseModel):
    """Optional caller-supplied signal on PRIOR recall hits.

    The MCP-client AI calls ``recall(...)`` once to get hits, then on
    its NEXT recall passes a ``feedback`` payload referring to those
    earlier hits. The signal drives the self-improvement tuner — see
    ``analysis/2026-06-03-recursive-self-improvement.md`` §2.1.

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


# ── observe ─────────────────────────────────────────────────────────────────


MAX_OBSERVE_ACTION_CHARS = 200
MAX_OBSERVE_SUBJECT_CHARS = 1_000
MAX_OBSERVE_RESULT_CHARS = 2_000
MAX_OBSERVE_EXTRAS_BYTES = 64 * 1024
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

    @field_validator("action")
    @classmethod
    def _action_not_blank(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            msg = "event.action must be a non-empty string"
            raise ValueError(msg)
        return stripped

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
