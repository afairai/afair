"""Tool handlers — pure business logic, no FastMCP dependency.

These functions are unit-testable directly without spinning up a server.
The MCP server wrapper in `server.py` registers them with FastMCP and
maps validation errors to MCP error responses.
"""

from __future__ import annotations

import base64
import binascii
import re
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from ..agents import read_latest_interpretation, schedule_extraction
from ..agents.binder import get_linked_event_ids
from ..agents.conflict_resolver import read_conflicts_batch, read_conflicts_for_event
from ..agents.embedding import EmbeddingError, embed_query
from ..agents.invalidation import (
    InvalidationInfo,
    read_invalidation,
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
    GetEventResult,
    InvalidateResult,
    InvalidationSummary,
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

# Background pool used to overlap the OpenAI embedding API call with the
# (cheap) local FTS query during recall. Pool is process-level so it
# survives across requests. max_workers=4 covers a handful of concurrent
# recalls; embedding is the dominant cost so this rarely saturates.
_RECALL_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="recall-parallel")


class ContentTooLargeError(ValueError):
    """Raised when remember content exceeds MAX_REMEMBER_BYTES."""


class InvalidBase64Error(ValueError):
    """Raised when BinaryContent.data_b64 isn't valid base64."""


class EventNotFoundError(LookupError):
    """Raised by ``get_event`` when neither event_id nor content_hash matches."""


class InvalidGetEventArgsError(ValueError):
    """Raised by ``get_event`` when both or neither selector is provided."""


class InvalidateTargetError(ValueError):
    """Raised when ``invalidate`` is called with a target that doesn't exist
    or is itself an invalidation event (no nested invalidations)."""


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
    invalidation: InvalidationInfo | None = None,
    conflicts: list[dict[str, Any]] | None = None,
) -> RecallHit:
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
        payload_summary=_payload_summary(event.payload),
        interpretation=interpretation,
        linked_event_ids=get_linked_event_ids(db, event.content_hash),
        invalidation=_invalidation_to_summary(invalidation),
        conflicts=conflict_flags,
    )


def _attach_invalidations(events: list[Event], db: Any) -> dict[str, InvalidationInfo]:
    """Batch-fetch invalidation info for a list of events.

    Returns a dict keyed by ``content_hash`` for use when building hits.
    Avoids N+1 lookups when recall returns many results.
    """
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
    """Return the right API key for the configured embedding model.

    Provider selection follows the model-string prefix (litellm convention)
    so this stays I5-neutral — adding a new provider is a settings change
    plus one line here.
    """
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
        # Unknown provider — try the OpenAI key as least-bad default; if
        # the call fails, the EmbeddingError fallback in recall serves
        # FTS-only results.
        key = ctx.openai_api_key
    return key.get_secret_value() if key is not None else None


# ── remember ────────────────────────────────────────────────────────────────


def remember(
    content: RememberContent,
    context: str | None = None,
    type_hint: str | None = None,
    parent_hashes: list[str] | None = None,
) -> RememberResult:
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
    already_existed = read_event_by_hash(db, preview_hash) is not None

    event = write_event(
        db,
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


# Compiled at import time; matches ULID-shaped strings (26 chars of the
# Crockford base32 alphabet, ULIDs are uppercase but be tolerant).
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$", re.IGNORECASE)
_IDENTIFIER_PREFIXES = ("sha256:", "http://", "https://", "file://")


def _auto_route_depth(query: str) -> Depth:
    """Pick the optimal recall depth without bothering the caller.

    Heuristics (stolen-and-improved from Cognee, who route between graph
    and vector; we route between FTS and hybrid since FastEmbed made the
    semantic path essentially free):

      - Exact identifiers (sha256: prefix, http(s) URLs, file:// paths,
        bare ULIDs): vector adds nothing because there is no semantic
        neighborhood. FTS exact-match wins on speed and precision.
      - Single tokens after FTS sanitization: usually a name, ID, or
        rare term; FTS5 already ranks these well, embedding similarity
        risks pulling in loosely-related noise.
      - Everything else: hybrid normal. Since FastEmbed turned the
        embedding cost into ~5ms in-process inference, the broader
        coverage is free.

    Returns the concrete depth ("shallow"|"normal") — never "auto" or
    "deep" so the downstream code only sees the resolved value.
    """
    stripped = query.strip()
    if not stripped:
        return "shallow"
    if any(stripped.startswith(p) for p in _IDENTIFIER_PREFIXES):
        return "shallow"
    if _ULID_RE.match(stripped):
        return "shallow"
    # Token count after FTS sanitization (mirrors search_fts's behavior).
    tokens = re.sub(r'[-+*"():^]', " ", stripped).split()
    if len(tokens) <= 1:
        return "shallow"
    return "normal"


def recall(
    query: str,
    scope: str | None = None,
    depth: Depth = "auto",
    limit: int = 20,
) -> RecallResult:
    """Retrieve relevant events.

    Depth semantics (Phase 2):
      - ``auto``    — system picks based on query shape (default).
                      Exact identifiers or single tokens → shallow;
                      multi-token natural language → normal hybrid.
                      Callers rarely need anything else.
      - ``shallow`` — FTS5 only. No LLM/embedding API call. Cheapest.
      - ``normal``  — Hybrid FTS5 + vector recall via Reciprocal Rank
                      Fusion. The embedding call runs concurrently with
                      the FTS query; cached query strings hit instantly.
      - ``deep``    — Not yet richer than normal; returns hybrid + note
                      until the Phase 3+ reasoning agent lands.
    """
    ctx = get_context()
    db = connect_for_thread()
    note: str | None = None
    depth_used: Depth = depth

    # Resolve auto BEFORE branching, so the downstream logic only sees
    # the concrete depth. Keep depth_used reflecting the resolved value
    # so the caller can observe which path actually ran.
    if depth == "auto":
        depth = _auto_route_depth(query)
        depth_used = depth

    if depth == "shallow" or not ctx.semantic_recall_enabled:
        events = search_fts(db, query, limit=limit)
        depth_used = "shallow"
    else:
        # Overlap embedding API call (network-bound, 100-300ms) with the
        # FTS query (CPU-bound, <10ms). If the embedding succeeds we merge
        # via RRF; if it fails we fall back to FTS-only without surfacing
        # the error to the AI client.
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
                invalidation=invalidations.get(e.content_hash),
                conflicts=conflicts.get(e.content_hash),
            )
            for e in events
        ],
        depth_used=depth_used,
        note=note,
    )


# ── list_context ────────────────────────────────────────────────────────────


def list_context(about: str | None = None, limit: int = 50) -> ListContextResult:
    db = connect_for_thread()

    if about:
        recent_events = search_fts(db, about, limit=limit)
    else:
        recent_events = list(iter_events(db, limit=limit))

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

    invalidations = _attach_invalidations(recent_events, db)
    conflicts = _attach_conflicts(recent_events, db)
    return ListContextResult(
        summary=ContextSummary(
            total_events=total,
            by_kind=by_kind,
            by_origin=by_origin,
            recent=[
                _event_to_hit(
                    e,
                    db,
                    invalidation=invalidations.get(e.content_hash),
                    conflicts=conflicts.get(e.content_hash),
                )
                for e in recent_events
            ],
        ),
    )


# ── get_event ───────────────────────────────────────────────────────────────


def _materialize_full_payload(payload: dict[str, Any], vault_dir: Any) -> dict[str, Any]:
    """Return a payload with ``text-large`` blobs read back into ``text``.

    The substrate stores large text in the object store referenced by
    ``blob_hash``. For ``get_event`` we surface the full text inline so
    callers see one consistent shape. Binary payloads are left as-is —
    the raw bytes stay in the object store; a future ``read_blob`` tool
    will expose them on explicit request.
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
        # Keep blob_hash + size for traceability; the caller can verify.
    except (OSError, UnicodeDecodeError) as e:
        materialized["text_unavailable"] = (
            f"failed to read object {blob_hash!s}: {type(e).__name__}: {e}"
        )
    return materialized


def get_event(
    event_id: str | None = None,
    content_hash: str | None = None,
) -> GetEventResult:
    """Return the FULL untruncated payload for one specific event.

    Counterpart to ``recall``, which caps each hit's payload at ~500 chars
    for skim-many-results UX. When a caller wants the actual content of
    one specific event (e.g., to display a long document), recall to find
    the candidate, then ``get_event(event_id=...)`` to fetch the whole thing.

    Exactly one of ``event_id`` or ``content_hash`` must be provided.
    """
    if (event_id is None) == (content_hash is None):
        msg = "get_event requires exactly one of event_id or content_hash"
        raise InvalidGetEventArgsError(msg)

    ctx = get_context()
    db = connect_for_thread()
    event = (
        read_event_by_id(db, event_id)
        if event_id is not None
        else read_event_by_hash(db, content_hash)  # type: ignore[arg-type]
    )
    if event is None:
        selector = (
            f"event_id={event_id!r}" if event_id is not None else f"content_hash={content_hash!r}"
        )
        msg = f"no event found for {selector}"
        raise EventNotFoundError(msg)

    payload = _materialize_full_payload(event.payload, ctx.vault_dir)

    interp = read_latest_interpretation(db, event.content_hash)
    interpretation: dict[str, Any] | None = (
        _interpretation_summary(interp.extraction) if interp is not None else None
    )

    return GetEventResult(
        event_id=event.id,
        content_hash=event.content_hash,
        created_at=event.created_at,
        kind=event.kind,
        origin=event.origin,
        payload=payload,
        interpretation=interpretation,
        linked_event_ids=get_linked_event_ids(db, event.content_hash),
        parent_hashes=list(event.parent_hashes or []),
        invalidation=_invalidation_to_summary(read_invalidation(db, event.content_hash)),
        conflicts=[ConflictFlag(**c) for c in read_conflicts_for_event(db, event.content_hash)],
    )


# ── invalidate ──────────────────────────────────────────────────────────────


def invalidate(target_hash: str, reason: str | None = None) -> InvalidateResult:
    """Mark a substrate event as superseded by later evidence.

    Append-only bi-temporal model (stolen from Graphiti, implemented at
    the substrate layer instead of in a graph DB):

      - The target event is NOT touched. I2 forbids it.
      - A new event with ``kind='invalidate'`` is written, with the
        target hash recorded in its payload and in ``parent_hashes`` for
        lineage.
      - Subsequent recall calls surface the target with a non-null
        ``invalidation`` field — but DO NOT filter it out. The AI
        client decides based on whether the query is about current
        state or history.
      - Re-validation is not modelled. To "undo" an invalidation,
        write a new fact and (optionally) invalidate the invalidation
        itself; latest-by-created_at wins on the read side.

    Rejects:
      - target_hash that doesn't resolve to any substrate event
      - target_hash that points at an existing invalidate event
        (no nested invalidations in v0; future Phase could add them)
    """
    db = connect_for_thread()

    target = read_event_by_hash(db, target_hash)
    if target is None:
        msg = f"no event found for target_hash={target_hash!r}"
        raise InvalidateTargetError(msg)
    if target.kind == "invalidate":
        msg = (
            f"target {target_hash!r} is itself an invalidation event; "
            "nested invalidations are not supported in v1"
        )
        raise InvalidateTargetError(msg)

    prior = read_invalidation(db, target_hash)
    already = prior is not None

    new_event = write_invalidation(
        db,
        target_hash=target_hash,
        reason=reason,
        origin=DEFAULT_ORIGIN,
    )
    return InvalidateResult(
        ok=True,
        event_id=new_event.id,
        content_hash=new_event.content_hash,
        target_hash=target_hash,
        target_already_invalidated=already,
    )


# ── observe ─────────────────────────────────────────────────────────────────


def observe(event: ObserveEvent) -> ObserveResult:
    db = connect_for_thread()

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
