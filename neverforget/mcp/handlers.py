"""Tool handlers — pure business logic, no FastMCP dependency.

These functions are unit-testable directly without spinning up a server.
The MCP server wrapper in `server.py` registers them with FastMCP and
maps validation errors to MCP error responses.
"""

from __future__ import annotations

import base64
import binascii
from typing import TYPE_CHECKING, Any

from ..agents import schedule_extraction
from ..substrate import (
    build_binary_payload,
    build_text_payload,
    iter_events,
    search_fts,
    write_event,
)
from .context import get_context
from .schemas import (
    MAX_REMEMBER_BYTES,
    ContextSummary,
    Depth,
    ListContextResult,
    ObserveEvent,
    ObserveResult,
    RecallHit,
    RecallResult,
    RememberContent,
    RememberResult,
    TextContent,
)

if TYPE_CHECKING:
    from ..substrate.events import Event

# All MCP-initiated events carry origin "agent" in v1. Per-client refinement
# (e.g., "agent:claude-code") happens server-side in a later phase by reading
# request headers — that change does not alter the MCP tool signatures and
# therefore does not violate I1.
DEFAULT_ORIGIN = "agent"

# Snippet length for text in recall/list_context summaries.
SUMMARY_TEXT_CHARS = 500


class ContentTooLargeError(ValueError):
    """Raised when remember content exceeds MAX_REMEMBER_BYTES."""


class InvalidBase64Error(ValueError):
    """Raised when BinaryContent.data_b64 isn't valid base64."""


# ── helpers ─────────────────────────────────────────────────────────────────


def _payload_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Build a truncation-safe view of a payload for recall results.

    Text bodies are capped at SUMMARY_TEXT_CHARS. Binary metadata passes
    through. Observe-event fields (action/subject/result) pass through
    verbatim. Unknown content_types still produce a reasonable summary.
    """
    content_type = payload.get("content_type", "unknown")
    summary: dict[str, Any] = {"content_type": content_type}

    if content_type == "text":
        text = payload.get("text", "")
        if isinstance(text, str):
            summary["text"] = text[:SUMMARY_TEXT_CHARS]
            summary["truncated"] = len(text) > SUMMARY_TEXT_CHARS
    elif content_type == "text-large":
        for k in ("blob_hash", "size_bytes", "mime"):
            if k in payload:
                summary[k] = payload[k]
    elif content_type == "binary":
        for k in ("blob_hash", "size_bytes", "mime", "filename_hint"):
            if k in payload:
                summary[k] = payload[k]
    elif content_type == "event":
        for k in ("action", "subject", "result"):
            if k in payload:
                summary[k] = payload[k]

    # Common metadata across content types
    for k in ("context", "type_hint", "language"):
        if payload.get(k) is not None:
            summary[k] = payload[k]

    return summary


def _event_to_hit(event: Event) -> RecallHit:
    return RecallHit(
        event_id=event.id,
        content_hash=event.content_hash,
        created_at=event.created_at,
        kind=event.kind,
        origin=event.origin,
        payload_summary=_payload_summary(event.payload),
    )


# ── remember ────────────────────────────────────────────────────────────────


def remember(
    content: RememberContent,
    context: str | None = None,
    type_hint: str | None = None,
    parent_hashes: list[str] | None = None,
) -> RememberResult:
    ctx = get_context()

    if isinstance(content, TextContent):
        encoded = content.text.encode("utf-8")
        if len(encoded) > MAX_REMEMBER_BYTES:
            msg = f"text content is {len(encoded)} bytes; max allowed in v1 is {MAX_REMEMBER_BYTES}"
            raise ContentTooLargeError(msg)
        payload = build_text_payload(
            text=content.text,
            context=context,
            type_hint=type_hint,
            vault_dir=ctx.vault_dir,
            inline_text_max_bytes=ctx.inline_text_max_bytes,
        )
    else:  # BinaryContent
        try:
            raw = base64.b64decode(content.data_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            msg = "data_b64 is not valid base64"
            raise InvalidBase64Error(msg) from e
        if len(raw) > MAX_REMEMBER_BYTES:
            msg = f"binary content is {len(raw)} bytes; max allowed in v1 is {MAX_REMEMBER_BYTES}"
            raise ContentTooLargeError(msg)
        payload = build_binary_payload(
            data=raw,
            mime=content.mime,
            filename_hint=content.filename_hint,
            context=context,
            type_hint=type_hint,
            vault_dir=ctx.vault_dir,
        )

    # Detect dedup BEFORE writing so we can report it in the result.
    # write_event() is idempotent on content_hash, so this is safe even if
    # concurrent calls race — both end up returning the same row.
    from ..substrate import content_hash as compute_content_hash
    from ..substrate import read_event_by_hash

    sorted_parents = sorted(parent_hashes) if parent_hashes else None
    preview_hash = compute_content_hash(
        kind="remember",
        origin=DEFAULT_ORIGIN,
        payload=payload,
        parent_hashes=sorted_parents,
    )
    already_existed = read_event_by_hash(ctx.db, preview_hash) is not None

    event = write_event(
        ctx.db,
        origin=DEFAULT_ORIGIN,
        kind="remember",
        payload=payload,
        parent_hashes=parent_hashes,
    )
    # Fire the warm-path Extractor — never on dedup (the existing event
    # already had its chance) and never blocking the user-facing tool call.
    if not already_existed:
        schedule_extraction(event.id)

    return RememberResult(
        ok=True,
        event_id=event.id,
        content_hash=event.content_hash,
        deduplicated=already_existed,
    )


# ── recall ──────────────────────────────────────────────────────────────────


def recall(
    query: str,
    scope: str | None = None,
    depth: Depth = "shallow",
    limit: int = 20,
) -> RecallResult:
    ctx = get_context()

    note: str | None = None
    if depth != "shallow":
        note = f"depth={depth!r} is not yet implemented (Phase 0); returning shallow results."

    events = search_fts(ctx.db, query, limit=limit)
    return RecallResult(
        hits=[_event_to_hit(e) for e in events],
        depth_used="shallow",
        note=note,
    )


# ── list_context ────────────────────────────────────────────────────────────


def list_context(about: str | None = None, limit: int = 50) -> ListContextResult:
    ctx = get_context()

    if about:
        recent_events = search_fts(ctx.db, about, limit=limit)
    else:
        recent_events = list(iter_events(ctx.db, limit=limit))

    total_row = ctx.db.execute("SELECT COUNT(*) FROM events").fetchone()
    total: int = total_row[0] if total_row else 0

    by_kind: dict[str, int] = {
        row["kind"]: row["c"]
        for row in ctx.db.execute("SELECT kind, COUNT(*) AS c FROM events GROUP BY kind")
    }
    by_origin: dict[str, int] = {
        row["origin"]: row["c"]
        for row in ctx.db.execute("SELECT origin, COUNT(*) AS c FROM events GROUP BY origin")
    }

    return ListContextResult(
        summary=ContextSummary(
            total_events=total,
            by_kind=by_kind,
            by_origin=by_origin,
            recent=[_event_to_hit(e) for e in recent_events],
        ),
    )


# ── observe ─────────────────────────────────────────────────────────────────


def observe(event: ObserveEvent) -> ObserveResult:
    ctx = get_context()

    # Dump back to a plain dict, preserving any extra fields the client set.
    event_dict = event.model_dump(exclude_none=False)
    payload: dict[str, Any] = {"content_type": "event", **event_dict}

    # Dedup-detection for parity with remember — same I3-clean idempotency.
    from ..substrate import content_hash as compute_content_hash
    from ..substrate import read_event_by_hash

    preview_hash = compute_content_hash(
        kind="observe",
        origin=DEFAULT_ORIGIN,
        payload=payload,
        parent_hashes=None,
    )
    already_existed = read_event_by_hash(ctx.db, preview_hash) is not None

    written = write_event(
        ctx.db,
        origin=DEFAULT_ORIGIN,
        kind="observe",
        payload=payload,
    )
    if not already_existed:
        schedule_extraction(written.id)

    return ObserveResult(
        ok=True,
        event_id=written.id,
        content_hash=written.content_hash,
    )
