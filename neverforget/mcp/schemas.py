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


Depth = Literal["shallow", "normal", "deep"]


class RecallHit(BaseModel):
    """One match returned by `recall`.

    ``payload_summary`` is the truncation-safe view of the raw substrate
    payload (text snippet, mime, etc.).
    ``interpretation`` is the latest successful Extractor output for the
    event when one is available — best_guess_kind, summary, entities,
    salient_facts, language, confidence, source_attribution. Optional;
    will be ``None`` for events whose extraction failed or is still in
    flight.
    """

    event_id: str
    content_hash: str
    created_at: str
    kind: str
    origin: str
    payload_summary: dict[str, Any]
    interpretation: dict[str, Any] | None = None


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
