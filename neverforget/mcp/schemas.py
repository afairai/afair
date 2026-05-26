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

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator

# 10 MB cap on `remember` content — v1 lock. Raising the cap later is
# additive (smaller clients still work); lowering would break I1.
MAX_REMEMBER_BYTES = 10 * 1024 * 1024
"""Raw-byte ceiling for a single `remember` call's content."""

# ── remember ────────────────────────────────────────────────────────────────


class TextContent(BaseModel):
    """Text variant of the remember content union."""

    type: Literal["text"]
    text: str


class BinaryContent(BaseModel):
    """Binary variant of the remember content union.

    `data_b64` is base64 of the raw bytes. `mime` is required.
    """

    type: Literal["binary"]
    data_b64: str
    mime: str = Field(min_length=1)
    filename_hint: str | None = None


RememberContent = Annotated[
    TextContent | BinaryContent,
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
    """

    hits: list[RecallHit]
    depth_used: Depth
    note: str | None = None
    summary: ContextSummary | None = None


# ── observe ─────────────────────────────────────────────────────────────────


class ObserveEvent(BaseModel):
    """An agent-self-logged event. ``action`` is required; other keys are
    recognized or preserved verbatim.

    Configured to allow arbitrary additional fields so different AI clients
    can use whatever shape fits their mental model.
    """

    model_config = {"extra": "allow"}

    action: str = Field(min_length=1)
    subject: str | None = None
    result: str | None = None

    @field_validator("action")
    @classmethod
    def _action_not_blank(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            msg = "event.action must be a non-empty string"
            raise ValueError(msg)
        return stripped


class ObserveResult(BaseModel):
    ok: bool
    event_id: str
    content_hash: str
