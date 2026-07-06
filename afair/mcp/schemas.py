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
    TypeAdapter,
    ValidationError,
    WrapValidator,
    model_validator,
)


def _canon_json(obj: Any) -> str:
    """Cheap deterministic serializer for size-bound checks (not for storage)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _parse_json_dict(v: str) -> dict[str, Any] | None:
    """Return the decoded dict if ``v`` is a JSON-serialized object, else None.

    Some MCP clients JSON-stringify object-typed tool arguments before sending
    them (their arg serializer flattens nested objects to strings). Without this
    the write-first coercers only ever saw the raw string and stored the whole
    JSON blob as literal text/action. A string that parses to a non-dict
    (``"[1,2,3]"``, ``"42"``, ``"true"``) returns None so it falls through to the
    existing bare-string tolerance — nothing is lost either way.
    """
    try:
        parsed = json.loads(v)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


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

# Module-level adapter for the canonical union. Validating through this instead
# of the WrapValidator's ``handler`` gives DETERMINISTIC discriminated-union
# validation, independent of the parameter-context schema ordering. In the
# function-parameter (FieldInfo) context that FastMCP uses, pydantic hoists the
# union's ``Field(discriminator="type")`` OUTSIDE the WrapValidator, so the
# wrapped ``handler`` rebuilds the tagged union and rejects non-dicts/wrong tags
# before coercion can run — the exact bug this restore fixes. The bare adapter
# has no such ordering hazard.
_REMEMBER_CONTENT_ADAPTER: TypeAdapter[RememberContent] = TypeAdapter(RememberContent)


def ensure_remember_content(v: Any) -> RememberContent:
    """Write-first intake: a ``remember`` is never rejected on content shape.

    afair's substrate is append-only and re-interpretable (I2/I3), so the intake
    priority is to accept and persist; the extractor then derives structure from
    the raw event (I6). This normalises the client mistakes we actually see into
    the canonical union instead of raising a ValidationError that would silently
    drop the memory:

    - an already-validated model -> returned untouched (fast path).
    - a bare string -> a text event; but if the string is a JSON-serialized
      object (a client that stringified the ``content`` argument), it is decoded
      first and coerced like the equivalent dict, so the intended object is
      persisted rather than the raw JSON text.
    - a dict whose ``type`` isn't a content tag (e.g. an agent put its
      ``type_hint`` value like 'fact' into ``content.type``) but that carries
      ``text`` -> a text event with that text.
    - anything else that still fails to validate (e.g. ``type: 'binary'`` with no
      data) -> the raw payload serialised as text, so nothing is ever lost.
    """
    if isinstance(v, TextContent | BinaryContent | BlobRefContent | CompoundContent):
        return v
    if isinstance(v, str):
        parsed = _parse_json_dict(v)
        v = parsed if parsed is not None else {"type": "text", "text": v}
    if isinstance(v, dict) and v.get("type") not in _CONTENT_TAGS:
        text = v.get("text")
        v = {
            "type": "text",
            "text": text
            if isinstance(text, str)
            else json.dumps(v, ensure_ascii=False, sort_keys=True),
        }
    try:
        return _REMEMBER_CONTENT_ADAPTER.validate_python(v)
    except ValidationError:
        raw = (
            v
            if isinstance(v, str)
            else json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)
        )
        return _REMEMBER_CONTENT_ADAPTER.validate_python({"type": "text", "text": raw})


def _coerce_remember_content(v: Any, _handler: Any) -> RememberContent:
    """WrapValidator entry for the ``content`` parameter.

    ``_handler`` is deliberately unused: in the parameter context its wrapped
    schema is the widened ``RememberContent | str`` union (the ``str`` member we
    must never return), and its discriminator ordering is the landmine this fix
    escapes. All validation goes through :func:`ensure_remember_content`, which
    uses the bare module-level adapter instead.
    """
    return ensure_remember_content(v)


RememberContentInput = Annotated[
    RememberContent | str,
    WrapValidator(_coerce_remember_content),
]
"""The `remember` tool parameter type: the canonical union OR a string, wrapped
in a write-first coercion so malformed content is normalised (worst case: stored
as text) instead of rejected.

The ``| str`` does double duty: (a) it keeps ``RememberContent``'s
``Field(discriminator=...)`` NESTED inside a Union arg so pydantic can no longer
hoist the discriminator outside the WrapValidator in the function-parameter
context (the bug that disabled the coercer at the live call layer); (b) it
advertises the string alternative in the tool's inputSchema so clients that
JSON-stringify the argument, or pre-validate client-side, stop refusing it.

I1-additive: the advertised acceptance set is a strict SUPERSET — every payload
valid under the prior object-only schema stays valid, and a string alternative is
added. No parameter is renamed, removed, or narrowed."""


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

MAX_OBSERVE_EXTRAS_CONTAINERS = 200
"""Maximum dict/list containers an ``observe`` extras structure may hold
before it is flattened to a text rendering. A structure with more nesting
than this is either adversarial or an accidental serialization; instead of
REJECTING it (which silently drops the whole observation) we render it to
``extras_text`` and mark ``extras_truncated``. Counted on the PARSED
structure with an iterative walk, so a pathologically deep bomb can never
RecursionError the validator before we react."""

MAX_OBSERVE_EXTRA_VALUE_CHARS = 48 * 1024
"""Per-value truncation ceiling when shrinking oversized ``observe`` extras.
An over-64KB extras dict has its largest string values cut to this length
(largest-first) rather than being rejected. The trade is deliberate: a
client that stuffs an 80KB blob into an extra persists a ~48KB prefix +
``extras_truncated: True`` instead of losing the entire write. Kept below
``MAX_OBSERVE_EXTRAS_BYTES`` so one truncated value leaves headroom for the
remaining keys."""

_OBSERVE_FIELD_CAPS = {
    "action": MAX_OBSERVE_ACTION_CHARS,
    "subject": MAX_OBSERVE_SUBJECT_CHARS,
    "result": MAX_OBSERVE_RESULT_CHARS,
}
"""Per-field character caps for the write-first truncation in
``ObserveEvent._accept_first``. Over-long values are truncated to the cap
and the full original preserved under ``<field>_full`` rather than rejected."""
"""Caps for observe() inputs. ``extras`` is the free-form open dict; it is
truncated/flattened rather than rejected (see ``_bound_extras``) so an
oversized or deeply-nested extras never drops the observation, while still
bounding what reaches the FTS index / SQLite payload row."""


def _stringify(value: Any) -> str:
    """Coerce a non-string value to a stable string for storage, never raising.

    Used to coerce a non-string ``subject``/``result`` (e.g. an int or a
    nested object a client packed into the field) before the length check, so
    pydantic's ``str | None`` constraint can't reject the whole write. Falls
    back through ``str()`` and finally a literal marker so even a
    self-referential or ultra-deep value can't RecursionError the validator."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        )
    except (ValueError, TypeError, RecursionError):
        try:
            return str(value)
        except Exception:
            return "<unrenderable value>"


_OBSERVE_PRESERVED_KEYS: frozenset[str] = frozenset(
    {f"{field}_full" for field in _OBSERVE_FIELD_CAPS}
    | {f"{field}_full_client" for field in _OBSERVE_FIELD_CAPS}
)
"""EXACT preservation keys ``_truncate_long_fields`` writes to losslessly keep
an over-long ``action``/``subject``/``result``: exactly ``action_full``,
``subject_full``, ``result_full`` and their ``_full_client`` variants. An exact
allowlist (not a ``_full`` suffix wildcard) — a suffix match let ANY
client-supplied extra ending in ``_full`` (e.g. ``diff_full``) skip both the
container count and the byte cap, so a 10MB ``diff_full`` persisted verbatim."""


def _is_preserved_key(key: str) -> bool:
    """True only for the exact ``_OBSERVE_PRESERVED_KEYS``. These live in
    ``__pydantic_extra__`` but are NOT free-form extras — they mirror an
    already-accepted primary field, so the extras size/nesting bounding leaves
    them intact (or truncating them would defeat the preservation). They are
    still capped at ``MAX_REMEMBER_BYTES`` in ``_cap_preserved_extras`` so an
    observe can't exceed remember's ceiling via a ``_full`` field."""
    return key in _OBSERVE_PRESERVED_KEYS


def _count_containers(obj: Any, threshold: int) -> int:
    """Count dict/list containers in a parsed structure, iteratively.

    Explicit stack (no recursion) so a deeply-nested bomb cannot RecursionError
    before the caller can react, and early-exits once the count passes
    ``threshold`` (returning ``threshold + 1``) so a huge-but-shallow structure
    is cheap and a deep one is bounded."""
    count = 0
    stack: list[Any] = [obj]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            count += 1
            if count > threshold:
                return count
            stack.extend(current.values())
        elif isinstance(current, list):
            count += 1
            if count > threshold:
                return count
            stack.extend(current)
    return count


def _safe_render_extras(extras: dict[str, Any]) -> str:
    """Best-effort, recursion-safe text rendering of an extras dict, bounded to
    ``MAX_OBSERVE_EXTRA_VALUE_CHARS``. Never raises — a structure too deep to
    serialize falls back to a marker rather than dropping the write."""
    try:
        rendered = _canon_json(extras)
    except (ValueError, TypeError, RecursionError):
        try:
            rendered = str(extras)
        except Exception:
            rendered = "<unrenderable extras>"
    return rendered[:MAX_OBSERVE_EXTRA_VALUE_CHARS]


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

        A ``str`` that is a JSON-serialized object (a client that stringified the
        ``event`` argument) is decoded first and flows through the dict branch, so
        its ``action`` / ``subject`` / ``result`` parse correctly instead of the
        whole blob landing in ``action``.
        """
        if isinstance(data, str):
            parsed = _parse_json_dict(data)
            if parsed is not None:
                data = parsed  # fall through to the dict branch below
            else:
                return cls._truncate_long_fields({"action": data.strip() or "observed"})
        if isinstance(data, dict):
            coerced = dict(data)
            action = coerced.get("action")
            if not isinstance(action, str) or not action.strip():
                coerced["action"] = "observed"
            return cls._truncate_long_fields(coerced)
        dump = json.dumps(data, ensure_ascii=False, default=str)
        return cls._truncate_long_fields({"action": "observed", "result": dump})

    @staticmethod
    def _truncate_long_fields(data: dict[str, Any]) -> dict[str, Any]:
        """Truncate over-long ``action`` / ``subject`` / ``result`` to their
        caps, preserving the full original under ``<field>_full`` so the
        Field constraints never fire on real input and no data is lost.

        A non-string, non-None ``subject``/``result`` (an int, or a nested
        object a client packed into the field) is coerced to a string first,
        so pydantic's ``str | None`` constraint can't reject the whole write.
        (``action`` is already coerced to a string in ``_accept_first``.)"""
        for field, cap in _OBSERVE_FIELD_CAPS.items():
            value = data.get(field)
            if value is not None and not isinstance(value, str):
                value = _stringify(value)
                data[field] = value
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
        """Bound the size + nesting of the free-form extras dict WITHOUT ever
        rejecting the write (write-first intake, same principle as remember's
        content coercion — I1-additive: a strict superset of what used to be
        accepted).

        Pydantic stores extras (the keys beyond action/subject/result) in
        ``__pydantic_extra__``. An unbounded extras dict would inflate the FTS
        index and every recall hit's row deserialization cost, so:

        - too many containers (nesting/serialization bomb) → flatten the whole
          extras to ``{extras_text, extras_truncated: True}``;
        - otherwise over the byte cap → truncate the largest string values to
          ``MAX_OBSERVE_EXTRA_VALUE_CHARS`` (largest-first) and mark
          ``extras_truncated``; if still over (non-string bulk), flatten.

        Container-counting runs on the parsed structure BEFORE any serialize,
        so a >1000-deep bomb can't ``RecursionError`` ``_canon_json`` first.
        The previous version RAISED on either condition, which silently dropped
        the observation at the signature layer — the exact failure this fixes.
        """
        extras = self.__pydantic_extra__
        if not extras:
            return self

        # Preservation keys are exempt from the free-extras bounding, but still
        # capped at MAX_REMEMBER_BYTES so an observe can't exceed remember's
        # ceiling via a *_full field.
        self._cap_preserved_extras()

        # Bound only the genuinely free-form extras; the exact ``_full``
        # preservation keys are exempt (see _is_preserved_key).
        free = {k: v for k, v in extras.items() if not _is_preserved_key(k)}
        if not free:
            return self

        if _count_containers(free, MAX_OBSERVE_EXTRAS_CONTAINERS) > MAX_OBSERVE_EXTRAS_CONTAINERS:
            self._flatten_extras()
            return self

        if len(_canon_json(free)) <= MAX_OBSERVE_EXTRAS_BYTES:
            return self

        self._shrink_extras()
        free = {k: v for k, v in extras.items() if not _is_preserved_key(k)}
        if len(_canon_json(free)) > MAX_OBSERVE_EXTRAS_BYTES:
            self._flatten_extras()
        return self

    def _cap_preserved_extras(self) -> None:
        """Cap each exact ``_full`` preservation value at ``MAX_REMEMBER_BYTES``.

        The preservation keys are exempt from the free-extras size/nesting
        bounding (they mirror a primary field losslessly), but they must not let
        an ``observe`` smuggle content past remember's 10MB ceiling. An 80KB
        ``result_full`` passes untouched; a 10MB+ one is truncated to the cap and
        ``extras_truncated`` is marked."""
        extras = self.__pydantic_extra__
        if extras is None:
            return
        for key in _OBSERVE_PRESERVED_KEYS:
            value = extras.get(key)
            if isinstance(value, str) and len(value) > MAX_REMEMBER_BYTES:
                extras[key] = value[:MAX_REMEMBER_BYTES]
                extras["extras_truncated"] = True

    def _flatten_extras(self) -> None:
        """Replace the free-form extras with a bounded text rendering — the
        never-reject fallback for a structure too nested/large to store
        verbatim. Exact ``_full`` preservation keys are kept; best-effort render
        so nothing is dropped silently."""
        extras = self.__pydantic_extra__
        if extras is None:
            return
        free = {k: v for k, v in extras.items() if not _is_preserved_key(k)}
        text = _safe_render_extras(free)
        for key in [k for k in extras if not _is_preserved_key(k)]:
            del extras[key]
        extras["extras_text"] = text
        extras["extras_truncated"] = True

    def _shrink_extras(self) -> None:
        """Truncate the largest free-form string values to
        ``MAX_OBSERVE_EXTRA_VALUE_CHARS`` (largest-first) until the serialized
        free extras fit or no oversized string remains. Marks
        ``extras_truncated`` when anything was cut. Preservation keys are
        never touched."""
        extras = self.__pydantic_extra__
        if extras is None:
            return
        truncated_any = False
        string_items = sorted(
            ((k, v) for k, v in extras.items() if isinstance(v, str) and not _is_preserved_key(k)),
            key=lambda kv: len(kv[1]),
            reverse=True,
        )
        for key, value in string_items:
            if len(value) > MAX_OBSERVE_EXTRA_VALUE_CHARS:
                extras[key] = value[:MAX_OBSERVE_EXTRA_VALUE_CHARS]
                truncated_any = True
            free = {k: v for k, v in extras.items() if not _is_preserved_key(k)}
            if len(_canon_json(free)) <= MAX_OBSERVE_EXTRAS_BYTES:
                break
        if truncated_any:
            extras["extras_truncated"] = True


# Module-level adapter for the observe event. Everything routes through the
# model, which triggers ``_accept_first`` (the write-first + JSON-string
# coercion). Same deterministic-validation rationale as the remember adapter.
_OBSERVE_EVENT_ADAPTER: TypeAdapter[ObserveEvent] = TypeAdapter(ObserveEvent)


def ensure_observe_event(v: Any) -> ObserveEvent:
    """Write-first intake for ``observe``: never rejected on shape.

    Already-validated events pass through; everything else (dict, bare string,
    JSON-serialized object string) runs through the model so ``_accept_first``
    applies the action default, the JSON-string decode, and the AFAIR-H
    truncation. Mirrors :func:`ensure_remember_content`.
    """
    if isinstance(v, ObserveEvent):
        return v
    try:
        return _OBSERVE_EVENT_ADAPTER.validate_python(v)
    except ValidationError:
        # Last-resort never-drop fallback (mirrors ensure_remember_content):
        # if some pathological input still fails the model after the
        # write-first coercions + extras bounding, persist it under a default
        # action with the raw payload serialized into result rather than
        # dropping the observation at the signature layer.
        try:
            raw = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
        except (ValueError, TypeError, RecursionError):
            # Even the fallback serialization can fail (a self-referential or
            # ultra-deep object); never let that turn into a dropped write.
            raw = "<unserializable observe payload>"
        return _OBSERVE_EVENT_ADAPTER.validate_python({"action": "observed", "result": raw})


def _coerce_observe_event(v: Any, _handler: Any) -> ObserveEvent:
    """WrapValidator entry for the ``event`` parameter.

    ``_handler`` is unused for the same reason as the remember coercer: the
    parameter-context wrapped schema is the widened ``ObserveEvent | str`` union.
    Validation goes through :func:`ensure_observe_event`.
    """
    return ensure_observe_event(v)


ObserveEventInput = Annotated[
    ObserveEvent | str,
    WrapValidator(_coerce_observe_event),
]
"""The `observe` tool parameter type: the event model OR a string, wrapped in a
write-first coercion. The ``| str`` advertises the string alternative in the
tool's inputSchema (so a client that JSON-stringifies ``event`` is accepted) and,
as with ``remember``, keeps the wrap validator reliably in the validation path.

I1-additive: strict schema SUPERSET — the object form is unchanged, a string
alternative is added; no parameter renamed, removed, or narrowed."""


class ObserveResult(BaseModel):
    ok: bool
    event_id: str
    content_hash: str
