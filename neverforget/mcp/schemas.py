"""Pydantic schemas for MCP tool inputs and outputs.

These shapes are part of the v1 contract per Invariant I1. New optional
fields may be added forever; existing fields are locked.
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
    ok: bool
    event_id: str
    content_hash: str
    deduplicated: bool


# ── recall ──────────────────────────────────────────────────────────────────


Depth = Literal["auto", "shallow", "normal", "deep"]
"""Recall depth selector.

- ``auto``     — system picks based on the query shape (default since
                 2026-05-26 ; stolen from Cognee's auto-routing concept).
                 Exact identifiers, single words → shallow; otherwise normal.
                 Caller doesn't have to think about it.
- ``shallow``  — FTS5 keyword only. No embedding API call.
- ``normal``   — Hybrid FTS5 + vector via Reciprocal Rank Fusion.
- ``deep``     — Same as normal until the Phase 3+ reasoning agent lands.
"""


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
    """Event id of the invalidation — fetch via ``get_event`` for full
    reason + context."""

    reason: str | None = None


class RecallHit(BaseModel):
    """One match returned by `recall`.

    ``payload_summary`` is the truncation-safe view of the raw substrate
    payload (text snippet, mime, etc.).
    ``interpretation`` is the latest successful Extractor output for the
    event when one is available — best_guess_kind, summary, entities,
    salient_facts, language, confidence, source_attribution. Optional;
    will be ``None`` for events whose extraction failed or is still in
    flight.
    ``linked_event_ids`` are content_hashes the Bind agent automatically
    found to be semantically similar to this hit at extraction time.
    Useful for "show me events related to this one" — empty list when
    the Bind agent hasn't (yet) processed this event or found no
    neighbors.
    ``invalidation`` is non-null when a later event recorded a
    contradiction or supersession for this hit. Recall does NOT filter
    invalidated hits — they surface alongside current ones so the AI
    can decide based on query intent (current state vs history).
    """

    event_id: str
    content_hash: str
    created_at: str
    kind: str
    origin: str
    payload_summary: dict[str, Any]
    interpretation: dict[str, Any] | None = None
    linked_event_ids: list[str] = []
    invalidation: InvalidationSummary | None = None


class RecallResult(BaseModel):
    hits: list[RecallHit]
    depth_used: Depth
    note: str | None = None


# ── list_context ────────────────────────────────────────────────────────────


class ContextSummary(BaseModel):
    total_events: int
    by_kind: dict[str, int]
    by_origin: dict[str, int]
    recent: list[RecallHit]


class ListContextResult(BaseModel):
    summary: ContextSummary
    note: str | None = None


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


# ── get_event ───────────────────────────────────────────────────────────────


class GetEventResult(BaseModel):
    """Full untruncated payload for one event.

    Returned by ``get_event``. Unlike ``RecallHit.payload_summary`` which
    truncates text at ~500 chars for skim-many-results UX, this carries
    the entire payload. For ``content_type == "text-large"`` the inline
    text is read back from the object store and surfaced in ``payload.text``
    so callers see one consistent shape regardless of where bytes lived.
    For ``content_type == "binary"`` the bytes stay in the object store
    (use a future ``read_blob`` tool to fetch them); the payload still
    carries the metadata (mime, size, filename_hint, blob_hash).
    """

    event_id: str
    content_hash: str
    created_at: str
    kind: str
    origin: str
    payload: dict[str, Any]
    interpretation: dict[str, Any] | None = None
    linked_event_ids: list[str] = []
    parent_hashes: list[str] = []
    invalidation: InvalidationSummary | None = None


# ── invalidate ──────────────────────────────────────────────────────────────


class InvalidateResult(BaseModel):
    """Result of the ``invalidate`` MCP tool — bi-temporal supersession.

    Records that ``target_hash`` is no longer considered current. Both
    the target event and any prior invalidations remain in the substrate
    forever (I2); this just appends a new event marking the supersession.
    """

    ok: bool
    event_id: str
    """ULID of the new invalidation event."""

    content_hash: str
    """sha256 hash of the new invalidation event itself."""

    target_hash: str
    """The event_hash that this invalidation supersedes."""

    target_already_invalidated: bool
    """True if a prior invalidation already existed for the target.
    The new one becomes the current (latest-wins) record; the prior
    one stays in the substrate."""
