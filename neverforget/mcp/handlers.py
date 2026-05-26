"""Tool handlers — pure business logic, no FastMCP dependency.

These functions are unit-testable directly without spinning up a server.
The MCP server wrapper in `server.py` registers them with FastMCP and
maps validation errors to MCP error responses.

Three handlers, three verbs:
  - remember(content, *, context, type_hint, parent_hashes, invalidates)
  - recall(query=None, *, scope, depth, limit, by_id, by_content_hash,
           full_payload, stats)
  - observe(event)

Decision history: the surface was collapsed from 6 → 3 tools in
pre-release (2026-05-26, vault decision event 01KSHW6Q0EB1BBPKZ4Q2QT20NT)
to lock in the cleanest forever-API before any external user adopted it.
The old list_context, get_event, and invalidate verbs are absorbed:

  list_context(about=X, limit=N)
    →  recall(query=X, stats=True, limit=N)      (with X)
    →  recall(stats=True, limit=N)               (without X)

  get_event(event_id=X)
    →  recall(by_id=X, full_payload=True)

  get_event(content_hash=X)
    →  recall(by_content_hash=X, full_payload=True)

  invalidate(target_hash=X, reason=Y)
    →  remember(content={"type":"text","text":Y or "(no replacement)"},
                invalidates=[X])
"""

from __future__ import annotations

import base64
import binascii
import re
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from ..agents import read_latest_interpretation, schedule_extraction
from ..agents.binder import get_linked_event_ids
from ..agents.conflict_resolver import read_conflicts_batch
from ..agents.embedding import EmbeddingError, embed_query
from ..agents.invalidation import (
    INVALIDATE_KIND,
    InvalidationInfo,
    read_invalidations_batch,
    write_invalidation,
)
from ..substrate import (
    build_binary_payload,
    build_text_payload,
    iter_events,
    read_event_by_hash,
    read_event_by_id,
    read_object,
    rrf_merge,
    search_fts,
    search_vec,
    write_event,
)
from .context import connect_for_thread, get_context
from .schemas import (
    MAX_REMEMBER_BYTES,
    ConflictFlag,
    ContextSummary,
    Depth,
    InvalidationSummary,
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

# Snippet length for text in recall summaries (when full_payload=False).
SUMMARY_TEXT_CHARS = 500

# Background pool used to overlap the embedding call with the (cheap)
# local FTS query during recall. Pool is process-level so it survives
# across requests. max_workers=4 covers a handful of concurrent recalls.
_RECALL_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="recall-parallel")


class ContentTooLargeError(ValueError):
    """Raised when remember content exceeds MAX_REMEMBER_BYTES."""


class InvalidBase64Error(ValueError):
    """Raised when BinaryContent.data_b64 isn't valid base64."""


class InvalidRecallArgsError(ValueError):
    """Raised when recall is called with a contradictory mix of selectors
    (e.g., both ``by_id`` and ``by_content_hash`` provided)."""


class InvalidateTargetError(ValueError):
    """Raised when an invalidates target doesn't resolve to an event or
    points at an existing invalidation event (no nested invalidations)."""


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
    for k in ("context", "type_hint", "language", "target_hash", "reason"):
        if payload.get(k) is not None:
            summary[k] = payload[k]

    return summary


def _materialize_full_payload(payload: dict[str, Any], vault_dir: Any) -> dict[str, Any]:
    """Return a payload with ``text-large`` blobs read back into ``text``.

    The substrate stores large text in the object store referenced by
    ``blob_hash``. For ``full_payload=True`` calls we surface the full
    text inline so callers see one consistent shape. Binary payloads
    are left as-is — the raw bytes stay in the object store; the
    payload metadata (mime, size, filename_hint, blob_hash) is enough
    for most callers, and a dedicated blob-fetch capability is left
    for later phases.
    """
    content_type = payload.get("content_type")
    if content_type != "text-large":
        return dict(payload)

    blob_hash = payload.get("blob_hash")
    if not isinstance(blob_hash, str):
        return dict(payload)

    materialized = dict(payload)
    try:
        raw = read_object(vault_dir, blob_hash)
        materialized["text"] = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        materialized["text_unavailable"] = (
            f"failed to read object {blob_hash!s}: {type(e).__name__}: {e}"
        )
    return materialized


def _text_was_truncated(payload: dict[str, Any]) -> bool:
    """True iff the payload-summary form would clip the text."""
    if payload.get("content_type") != "text":
        return False
    text = payload.get("text", "")
    return isinstance(text, str) and len(text) > SUMMARY_TEXT_CHARS


_INTERPRETATION_SURFACE_KEYS = (
    "best_guess_kind",
    "summary",
    "entities",
    "salient_facts",
    "language",
    "confidence",
    "source_attribution",
)


def _interpretation_summary(extraction: dict[str, Any]) -> dict[str, Any] | None:
    """Pick the AI-useful subset of an Extractor's output for inclusion in
    a recall hit. Returns None when no useful fields are present.
    """
    surface: dict[str, Any] = {}
    for key in _INTERPRETATION_SURFACE_KEYS:
        if key in extraction and extraction[key] not in (None, "", [], {}):
            surface[key] = extraction[key]
    return surface or None


def _invalidation_to_summary(info: InvalidationInfo | None) -> InvalidationSummary | None:
    if info is None:
        return None
    return InvalidationSummary(at=info.at, by_event_id=info.by_event_id, reason=info.reason)


def _event_to_hit(
    event: Event,
    db: Any,
    *,
    full_payload: bool,
    invalidation: InvalidationInfo | None = None,
    conflicts: list[dict[str, Any]] | None = None,
) -> RecallHit:
    """Build one RecallHit. When ``full_payload`` is True the payload is
    materialized in full (text-large blobs read back from the object
    store); when False, the payload is the truncated summary view."""
    if full_payload:
        ctx = get_context()
        payload_view = _materialize_full_payload(event.payload, ctx.vault_dir)
        truncated = False
    else:
        payload_view = _payload_summary(event.payload)
        truncated = _text_was_truncated(event.payload)

    interp = read_latest_interpretation(db, event.content_hash)
    interpretation: dict[str, Any] | None = (
        _interpretation_summary(interp.extraction) if interp is not None else None
    )
    conflict_flags: list[ConflictFlag] = [ConflictFlag(**c) for c in (conflicts or [])]
    return RecallHit(
        event_id=event.id,
        content_hash=event.content_hash,
        created_at=event.created_at,
        kind=event.kind,
        origin=event.origin,
        payload=payload_view,
        truncated=truncated,
        interpretation=interpretation,
        linked_event_ids=get_linked_event_ids(db, event.content_hash),
        parent_hashes=list(event.parent_hashes or []),
        invalidation=_invalidation_to_summary(invalidation),
        conflicts=conflict_flags,
    )


def _attach_invalidations(events: list[Event], db: Any) -> dict[str, InvalidationInfo]:
    """Batch-fetch invalidation info for a list of events."""
    if not events:
        return {}
    hashes = [e.content_hash for e in events]
    return read_invalidations_batch(db, hashes)


def _attach_conflicts(events: list[Event], db: Any) -> dict[str, list[dict[str, Any]]]:
    """Batch-fetch conflict-resolver verdicts for a list of events."""
    if not events:
        return {}
    hashes = [e.content_hash for e in events]
    return read_conflicts_batch(db, hashes)


def _api_key_for_embedding(ctx: Any) -> str | None:
    """Return the right API key for the configured embedding model."""
    model = ctx.embedding_model
    key = None
    if model.startswith("openai/"):
        key = ctx.openai_api_key
    elif model.startswith("voyage/"):
        key = ctx.voyage_api_key
    elif model.startswith("gemini/"):
        key = ctx.gemini_api_key
    elif model.startswith("anthropic/"):
        key = ctx.anthropic_api_key
    else:
        # Unknown provider — try the OpenAI key as least-bad default.
        key = ctx.openai_api_key
    return key.get_secret_value() if key is not None else None


# ── remember ────────────────────────────────────────────────────────────────


def remember(
    content: RememberContent,
    context: str | None = None,
    type_hint: str | None = None,
    parent_hashes: list[str] | None = None,
    invalidates: list[str] | None = None,
) -> RememberResult:
    """Write a fact to the substrate, optionally invalidating prior facts.

    The ``invalidates`` kwarg supersedes prior facts in the same call
    (bi-temporal correction). Each target hash gets its own invalidation
    event with ``kind='invalidate'`` and ``parent_hashes=[target]`` for
    lineage. The new ``content`` is written first; invalidations follow.
    """
    ctx = get_context()
    db = connect_for_thread()

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
    from ..substrate import content_hash as compute_content_hash

    sorted_parents = sorted(parent_hashes) if parent_hashes else None
    preview_hash = compute_content_hash(
        kind="remember",
        origin=DEFAULT_ORIGIN,
        payload=payload,
        parent_hashes=sorted_parents,
    )
    already_existed = read_event_by_hash(db, preview_hash) is not None

    event = write_event(
        db,
        origin=DEFAULT_ORIGIN,
        kind="remember",
        payload=payload,
        parent_hashes=parent_hashes,
    )
    if not already_existed:
        schedule_extraction(event.id)

    # Process invalidations AFTER the main write. Each target must:
    #   - exist in the substrate
    #   - not be itself an invalidation event (no nested invalidations)
    invalidated_ok: list[str] = []
    for target_hash in invalidates or []:
        target = read_event_by_hash(db, target_hash)
        if target is None:
            msg = f"invalidates target not found: {target_hash!r}"
            raise InvalidateTargetError(msg)
        if target.kind == INVALIDATE_KIND:
            msg = (
                f"invalidates target {target_hash!r} is itself an "
                "invalidation event; nested invalidations are not supported"
            )
            raise InvalidateTargetError(msg)
        # Reason: pull from the new event's content if text, else generic.
        reason = (
            content.text if isinstance(content, TextContent) else f"superseded by event {event.id}"
        )
        write_invalidation(db, target_hash=target_hash, reason=reason, origin=DEFAULT_ORIGIN)
        invalidated_ok.append(target_hash)

    return RememberResult(
        ok=True,
        event_id=event.id,
        content_hash=event.content_hash,
        deduplicated=already_existed,
        invalidated=invalidated_ok,
    )


# ── recall ──────────────────────────────────────────────────────────────────


# Compiled at import time; matches ULID-shaped strings.
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$", re.IGNORECASE)
_IDENTIFIER_PREFIXES = ("sha256:", "http://", "https://", "file://")


def _auto_route_depth(query: str) -> Depth:
    """Pick the optimal recall depth without bothering the caller."""
    stripped = query.strip()
    if not stripped:
        return "shallow"
    if any(stripped.startswith(p) for p in _IDENTIFIER_PREFIXES):
        return "shallow"
    if _ULID_RE.match(stripped):
        return "shallow"
    tokens = re.sub(r'[-+*"():^]', " ", stripped).split()
    if len(tokens) <= 1:
        return "shallow"
    return "normal"


def recall(
    query: str | None = None,
    scope: str | None = None,
    depth: Depth = "auto",
    limit: int = 20,
    by_id: str | None = None,
    by_content_hash: str | None = None,
    full_payload: bool = False,
    stats: bool = False,
) -> RecallResult:
    """Read the vault. Six call modes share this signature:

      recall(query=...)                          → semantic search
      recall(by_id=...)                          → single-event lookup
      recall(by_content_hash=...)                → single-event lookup
      recall(stats=True)                         → summary + recent hits
      recall(query=..., full_payload=True)       → search, untruncated
      recall()                                   → most-recent N hits

    ``stats=True`` can combine with any of the above to add a
    ContextSummary to the response.

    Lookup modes (``by_id``/``by_content_hash``) imply ``full_payload=True``
    semantically — when you ask for one specific event you want it whole.

    ``scope`` is a free-text substring matched against the interpretation's
    topic_signal (Phase 3.5 — currently no-op until topic_signal lands).
    """
    if by_id is not None and by_content_hash is not None:
        msg = "recall accepts at most one of by_id, by_content_hash"
        raise InvalidRecallArgsError(msg)

    db = connect_for_thread()
    ctx = get_context()
    note: str | None = None

    summary: ContextSummary | None = None
    if stats:
        summary = _build_stats_summary(db)

    # ── Single-event lookup mode ───────────────────────────────────────────
    if by_id is not None or by_content_hash is not None:
        target = (
            read_event_by_id(db, by_id)
            if by_id is not None
            else read_event_by_hash(db, by_content_hash)  # type: ignore[arg-type]
        )
        if target is None:
            selector = (
                f"by_id={by_id!r}" if by_id is not None else f"by_content_hash={by_content_hash!r}"
            )
            return RecallResult(
                hits=[],
                depth_used="shallow",
                note=f"no event found for {selector}",
                summary=summary,
            )
        invalidations = _attach_invalidations([target], db)
        conflicts = _attach_conflicts([target], db)
        return RecallResult(
            hits=[
                _event_to_hit(
                    target,
                    db,
                    full_payload=True,  # lookup-by-id always returns the full event
                    invalidation=invalidations.get(target.content_hash),
                    conflicts=conflicts.get(target.content_hash),
                )
            ],
            depth_used="shallow",
            summary=summary,
        )

    # ── Search / browse mode ───────────────────────────────────────────────
    events: list[Event]
    depth_used: Depth = depth

    if query is None or not query.strip():
        # No query, no by_id — return most-recent N events (browse mode).
        events = list(iter_events(db, limit=limit))
        depth_used = "shallow"
    else:
        # Real query — resolve depth, run FTS+vec if normal.
        if depth == "auto":
            depth = _auto_route_depth(query)
            depth_used = depth

        if depth == "shallow" or not ctx.semantic_recall_enabled:
            events = search_fts(db, query, limit=limit)
            depth_used = "shallow"
        else:
            api_key = _api_key_for_embedding(ctx)
            emb_future = _RECALL_POOL.submit(
                embed_query, model=ctx.embedding_model, text=query, api_key=api_key
            )
            fts_hits = search_fts(db, query, limit=limit)
            try:
                embedding = emb_future.result()
            except EmbeddingError:
                events = fts_hits
                depth_used = "shallow"
                note = "semantic recall unavailable; returned FTS-only results"
            else:
                vec_hits = search_vec(db, embedding, limit=limit)
                events = rrf_merge(fts_hits, vec_hits, limit=limit)
                depth_used = "normal"
                if depth == "deep":
                    note = (
                        "deep depth is not yet richer than normal "
                        "(Phase 3+ reasoning agent pending); returned hybrid results"
                    )

    invalidations = _attach_invalidations(events, db)
    conflicts = _attach_conflicts(events, db)
    return RecallResult(
        hits=[
            _event_to_hit(
                e,
                db,
                full_payload=full_payload,
                invalidation=invalidations.get(e.content_hash),
                conflicts=conflicts.get(e.content_hash),
            )
            for e in events
        ],
        depth_used=depth_used,
        note=note,
        summary=summary,
    )


def _build_stats_summary(db: Any) -> ContextSummary:
    """Compute the vault-wide totals + breakdowns for stats=True."""
    total_row = db.execute("SELECT COUNT(*) FROM events").fetchone()
    total: int = total_row[0] if total_row else 0
    by_kind: dict[str, int] = {
        row["kind"]: row["c"]
        for row in db.execute("SELECT kind, COUNT(*) AS c FROM events GROUP BY kind")
    }
    by_origin: dict[str, int] = {
        row["origin"]: row["c"]
        for row in db.execute("SELECT origin, COUNT(*) AS c FROM events GROUP BY origin")
    }
    return ContextSummary(total_events=total, by_kind=by_kind, by_origin=by_origin)


# ── observe ─────────────────────────────────────────────────────────────────


def observe(event: ObserveEvent) -> ObserveResult:
    db = connect_for_thread()

    event_dict = event.model_dump(exclude_none=False)
    payload: dict[str, Any] = {"content_type": "event", **event_dict}

    from ..substrate import content_hash as compute_content_hash

    preview_hash = compute_content_hash(
        kind="observe",
        origin=DEFAULT_ORIGIN,
        payload=payload,
        parent_hashes=None,
    )
    already_existed = read_event_by_hash(db, preview_hash) is not None

    written = write_event(
        db,
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
