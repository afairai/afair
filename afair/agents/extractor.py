"""The warm-path Extractor agent.

Triggered after each successful ``remember`` or ``observe`` write. Runs the
LLM call on a background thread so the user-facing tool call returns
immediately. Produces one Interpretation row per event — success or failure
(option (b)): failures are themselves stored, so retry and diagnosis are
observable without touching the substrate.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import structlog

from ..substrate import object_path, open_db, read_object
from ..substrate import pipeline_events as pe
from ..substrate.events import read_event_by_id
from .binary_extractors import (
    AudioTranscriptionError,
    ImageDescriptionError,
    PdfExtractionError,
    describe_image,
    extract_pdf_text,
    modality_for_mime,
    transcribe_audio,
)
from .binder import find_and_record_links
from .embedding import EmbeddingError, embed_text, serialize_vector
from .interpretation import (
    write_failed_interpretation,
    write_interpretation,
)
from .llm import LLMError, LLMResponseError, call_tool
from .prompts import (
    EXTRACTOR_SCHEMA_VERSION,
    EXTRACTOR_SYSTEM_PROMPT,
    EXTRACTOR_TOOL_DESCRIPTION,
    EXTRACTOR_TOOL_NAME,
    EXTRACTOR_TOOL_SCHEMA,
    build_user_message,
)

# Thread-local DB connection for the extractor pool. See
# _conn_for_extractor_thread for the rationale (Perf audit I3).
_extractor_thread_local = threading.local()

# NOTE: `from ..mcp.context import get_context` is intentionally NOT at the
# top of the module. mcp/__init__.py loads server.py which loads handlers.py
# which imports back from agents — leading to a circular import at startup.
# Importing get_context lazily inside the entry-point functions breaks the
# cycle without changing the runtime call graph.

if TYPE_CHECKING:
    from pathlib import Path

log = structlog.get_logger(__name__)

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="extractor")
"""Background workers — text-only Phase 0 load fits easily in 4 threads."""

# Shut down cleanly on interpreter exit so pending extractions try to land.
atexit.register(lambda: _EXECUTOR.shutdown(wait=True, cancel_futures=False))


def _api_key_for(model: str, ctx: object) -> str | None:
    """Return the right secret-value key for a given litellm-style model string."""
    if model.startswith("openai/"):
        key = getattr(ctx, "openai_api_key", None)
    elif model.startswith("gemini/"):
        key = getattr(ctx, "gemini_api_key", None)
    else:
        key = getattr(ctx, "anthropic_api_key", None)
    return key.get_secret_value() if key is not None else None


def _capture_extractor_kwargs(ctx: Any) -> dict[str, Any]:
    """Snapshot the per-call settings the background extractor needs.

    Captured at submit time so the worker thread doesn't reach back into
    the live ServerContext (which could mutate between submit + run).
    """
    return {
        "vault_dir": ctx.vault_dir,
        "model": ctx.extractor_model,
        "api_key": _api_key_for(ctx.extractor_model, ctx),
        "embedding_model": ctx.embedding_model,
        "embedding_api_key": _api_key_for(ctx.embedding_model, ctx),
        "embedding_dim": ctx.embedding_dim,
        "semantic_recall_enabled": ctx.semantic_recall_enabled,
        "vision_model": ctx.vision_model,
        "vision_api_key": _api_key_for(ctx.vision_model, ctx),
        "transcription_model": ctx.transcription_model,
        "transcription_api_key": _api_key_for(ctx.transcription_model, ctx),
    }


def schedule_extraction(event_id: str) -> None:
    """Fire-and-forget the extraction for ``event_id``. Returns immediately.

    Reads the active ServerContext synchronously to capture the model name,
    vault path, and API keys; then dispatches the actual work to the
    background pool. The background thread opens its own SQLite connection,
    so this is safe even if the main connection is busy.
    """
    from ..mcp.context import get_context  # lazy — breaks circular import

    ctx = get_context()
    from ..mcp.context import connect_for_thread

    try:
        _EXECUTOR.submit(_run_extraction, event_id=event_id, **_capture_extractor_kwargs(ctx))
    except RuntimeError as exc:
        pe.record_safe(
            connect_for_thread,
            event_id=event_id,
            stage=pe.STAGE_EXTRACTION_ENQUEUED,
            status=pe.STATUS_FAILED,
            producer="schedule_extraction",
            detail=f"submit_after_shutdown: {exc}",
        )
        # Interpreter teardown: atexit fired _EXECUTOR.shutdown() while
        # we were mid-request. The event row is already durable; a future
        # boot's backfill (or the cold-path interpretation re-runs) will
        # pick this up. Don't propagate — the MCP client already got
        # its 200 OK back from remember() (audit finding — concurrency
        # bug #3, latent during clean shutdown).
        log.info(
            "extractor.submit_after_shutdown",
            event_id=event_id,
            detail=str(exc),
        )
        return
    pe.record_safe(
        connect_for_thread,
        event_id=event_id,
        stage=pe.STAGE_EXTRACTION_ENQUEUED,
        producer="schedule_extraction",
    )


def extract_sync(event_id: str) -> None:
    """Synchronous extraction — for tests and debugging.

    Same code path as the fire-and-forget variant but blocks until done.
    """
    from ..mcp.context import get_context  # lazy — breaks circular import

    ctx = get_context()
    _run_extraction(event_id=event_id, **_capture_extractor_kwargs(ctx))


def _conn_for_extractor_thread(vault_dir: Path, embedding_dim: int) -> Any:
    """Reuse one SQLite connection per ThreadPoolExecutor worker thread.

    ``open_db`` is not free: it runs ~30 idempotent DDL statements, loads
    the sqlite-vec extension, runs PRAGMA optimize. Pre-Perf-audit-I3
    we paid this cost (~10-30ms) on every event extraction. At 30 events/s
    sustained that's ~600ms/sec of pure CPU spent opening connections —
    ~60% of one CPU on shared-cpu-1x. Holding a thread-local connection
    for the worker's lifetime drops this to one open per worker per
    process lifetime.

    NB: cached connection is keyed only by thread, not by vault_dir.
    Production has one vault per process so this is fine. Tests that
    reuse the executor across different ``tmp_path`` fixtures MUST call
    ``clear_extractor_thread_db()`` between cases (the test conftest /
    ``clear_context`` already does this).
    """
    conn = getattr(_extractor_thread_local, "db", None)
    cached_vault = getattr(_extractor_thread_local, "vault_dir", None)
    # If the cached connection is for a different vault_dir (test mode
    # switching tmp_paths), drop it and open fresh. Production never
    # hits this branch.
    if conn is not None and cached_vault != vault_dir:
        with contextlib.suppress(Exception):
            conn.close()
        conn = None
    if conn is None:
        conn = open_db(vault_dir, embedding_dim=embedding_dim)
        _extractor_thread_local.db = conn
        _extractor_thread_local.vault_dir = vault_dir
    return conn


def clear_extractor_thread_db() -> None:
    """Close + drop the thread-local extractor DB connection.

    Used by tests' clear_context() to ensure each fixture starts with a
    fresh connection to its own ``tmp_path``. Idempotent and silent if
    no connection is cached.
    """
    conn = getattr(_extractor_thread_local, "db", None)
    if conn is not None:
        with contextlib.suppress(Exception):
            conn.close()
        del _extractor_thread_local.db
    if hasattr(_extractor_thread_local, "vault_dir"):
        del _extractor_thread_local.vault_dir


def _run_extraction(
    *,
    event_id: str,
    vault_dir: Path,
    model: str,
    api_key: str | None,
    embedding_model: str = "openai/text-embedding-3-small",
    embedding_api_key: str | None = None,
    embedding_dim: int = 1536,
    semantic_recall_enabled: bool = True,
    vision_model: str = "anthropic/claude-haiku-4-5",
    vision_api_key: str | None = None,
    transcription_model: str = "openai/whisper-1",
    transcription_api_key: str | None = None,
) -> None:
    """The actual extraction work. Reuses a thread-local DB connection.

    Modality dispatch (Phase 1 multi-modal):
      - text / text-large / observe-event  → text-LLM tool-use call
      - binary + mime=application/pdf       → pypdf text extract, then text-LLM
      - binary + mime=audio/*               → whisper transcript, then text-LLM
      - binary + mime=image/*               → vision-LLM tool-use call

    All paths converge on a single ``write_interpretation`` row carrying
    the same JSON shape so recall + downstream agents stay modality-blind.
    """
    db = _conn_for_extractor_thread(vault_dir, embedding_dim)
    event = read_event_by_id(db, event_id)
    if event is None:
        log.warning("extractor.event_missing", event_id=event_id)
        pe.record(
            db,
            event_id=event_id,
            stage=pe.STAGE_EXTRACTION_STARTED,
            status=pe.STATUS_FAILED,
            producer=f"extractor:{model}",
            detail="event row not found",
        )
        return
    pe.record(
        db,
        event_id=event_id,
        event_hash=event.content_hash,
        stage=pe.STAGE_EXTRACTION_STARTED,
        producer=f"extractor:{model}",
    )

    payload = event.payload or {}
    content_type = payload.get("content_type")
    mime = payload.get("mime") if isinstance(payload, dict) else None
    modality = (
        modality_for_mime(mime if isinstance(mime, str) else None)
        if content_type == "binary"
        else "text"
    )

    extracted_text: str | None = None
    extractor_subtag = "text"

    # Pre-LLM binary extraction. PDF + audio produce text that's fed to
    # the standard text-LLM call (so the extraction schema stays the same).
    # Image is handled by the vision-LLM branch below instead.
    if modality == "pdf":
        blob_hash = payload.get("blob_hash") if isinstance(payload, dict) else None
        if isinstance(blob_hash, str):
            try:
                extracted_text = extract_pdf_text(object_path(vault_dir, blob_hash))
                extractor_subtag = "pdf"
                log.info(
                    "extractor.pdf_text",
                    event_id=event_id,
                    chars=len(extracted_text),
                )
            except PdfExtractionError as e:
                log.warning("extractor.pdf_failed", event_id=event_id, error=str(e))
                write_failed_interpretation(
                    db,
                    event=event,
                    version=EXTRACTOR_SCHEMA_VERSION,
                    produced_by=f"extractor:pdf:{model}",
                    error_type="pdf_extraction_error",
                    error_message=str(e),
                )
                return
    elif modality == "audio":
        blob_hash = payload.get("blob_hash") if isinstance(payload, dict) else None
        if isinstance(blob_hash, str):
            try:
                extracted_text = transcribe_audio(
                    path=object_path(vault_dir, blob_hash),
                    model=transcription_model,
                    api_key=transcription_api_key,
                )
                extractor_subtag = "audio"
                log.info(
                    "extractor.audio_transcript",
                    event_id=event_id,
                    chars=len(extracted_text),
                    model=transcription_model,
                )
            except AudioTranscriptionError as e:
                log.warning("extractor.audio_failed", event_id=event_id, error=str(e))
                write_failed_interpretation(
                    db,
                    event=event,
                    version=EXTRACTOR_SCHEMA_VERSION,
                    produced_by=f"extractor:audio:{transcription_model}",
                    error_type="audio_transcription_error",
                    error_message=str(e),
                )
                return

    # text-large: the body spilled to the object store, so the payload the
    # LLM would otherwise see carries only a blob_hash. Rehydrate it the same
    # way PDF/audio yield text — read the blob back as the extracted_text. One
    # read closes three holes at once: the extraction now runs on the real
    # body, _embedding_text_for_event embeds it, and _enrich_fts_after_extraction
    # indexes it. Without this, a >inline-threshold paste is invisible to the
    # entire intelligence layer.
    if content_type == "text-large" and extracted_text is None:
        blob_hash = payload.get("blob_hash") if isinstance(payload, dict) else None
        if isinstance(blob_hash, str):
            try:
                extracted_text = read_object(vault_dir, blob_hash).decode("utf-8")
                extractor_subtag = "text-large"
                log.info(
                    "extractor.text_large_rehydrated",
                    event_id=event_id,
                    chars=len(extracted_text),
                )
            except (OSError, ValueError, UnicodeDecodeError) as e:
                # Blob unreadable (missing / corrupt / not UTF-8). Record the
                # failure and return, like the PDF/audio paths: extracting from
                # metadata only would also make _enrich_fts_after_extraction
                # rewrite the FTS row WITHOUT the body, discarding the full text
                # the write path already indexed via searchable_body. Returning
                # leaves that write-time row intact, so the paste stays findable.
                log.warning(
                    "extractor.text_large_failed",
                    event_id=event_id,
                    error=str(e),
                )
                write_failed_interpretation(
                    db,
                    event=event,
                    version=EXTRACTOR_SCHEMA_VERSION,
                    produced_by=f"extractor:text-large:{model}",
                    error_type="text_large_read_error",
                    error_message=str(e),
                )
                return

    # Pick the LLM path based on modality. Image goes through a vision-
    # capable model with the image as a content part; everything else
    # goes through the text-tool-use path with optional pre-extracted
    # text augmenting the user message.
    if modality == "image":
        blob_hash = payload.get("blob_hash") if isinstance(payload, dict) else None
        if not isinstance(blob_hash, str) or not isinstance(mime, str):
            log.warning("extractor.image_missing_blob", event_id=event_id)
            write_failed_interpretation(
                db,
                event=event,
                version=EXTRACTOR_SCHEMA_VERSION,
                produced_by=f"extractor:image:{vision_model}",
                error_type="image_payload_error",
                error_message="image payload missing blob_hash or mime",
            )
            return
        produced_by = f"extractor:image:{vision_model}"
        try:
            extraction = describe_image(
                path=object_path(vault_dir, blob_hash),
                mime=mime,
                user_message=build_user_message(event),
                system_prompt=EXTRACTOR_SYSTEM_PROMPT,
                tool_name=EXTRACTOR_TOOL_NAME,
                tool_description=EXTRACTOR_TOOL_DESCRIPTION,
                tool_schema=EXTRACTOR_TOOL_SCHEMA,
                model=vision_model,
                api_key=vision_api_key,
            )
        except ImageDescriptionError as e:
            log.warning("extractor.image_failed", event_id=event_id, error=str(e))
            write_failed_interpretation(
                db,
                event=event,
                version=EXTRACTOR_SCHEMA_VERSION,
                produced_by=produced_by,
                error_type="image_description_error",
                error_message=str(e),
            )
            return
        validated_or_error = _validate_extraction(extraction)
    else:
        produced_by = (
            f"extractor:{extractor_subtag}:{model}" if extracted_text else f"extractor:{model}"
        )
        try:
            result = call_tool(
                model=model,
                system=EXTRACTOR_SYSTEM_PROMPT,
                user=build_user_message(event, extracted_text=extracted_text),
                tool_name=EXTRACTOR_TOOL_NAME,
                tool_description=EXTRACTOR_TOOL_DESCRIPTION,
                tool_schema=EXTRACTOR_TOOL_SCHEMA,
                api_key=api_key,
            )
        except LLMError as e:
            log.warning(
                "extractor.llm_error",
                event_id=event_id,
                error_type=e.error_type,
                error=str(e),
                model=model,
            )
            write_failed_interpretation(
                db,
                event=event,
                version=EXTRACTOR_SCHEMA_VERSION,
                produced_by=produced_by,
                error_type=e.error_type,
                error_message=str(e),
            )
            return
        validated_or_error = _validate_extraction(result.data)

    # Validate the extraction has at least the mandatory top-level keys.
    if isinstance(validated_or_error, str):
        log.warning(
            "extractor.validation_error",
            event_id=event_id,
            error=validated_or_error,
            model=model,
            modality=modality,
        )
        write_failed_interpretation(
            db,
            event=event,
            version=EXTRACTOR_SCHEMA_VERSION,
            produced_by=produced_by,
            error_type=LLMResponseError.error_type,
            error_message=validated_or_error,
        )
        pe.record(
            db,
            event_id=event_id,
            event_hash=event.content_hash,
            stage=pe.STAGE_EXTRACTION_FAILED,
            status=pe.STATUS_FAILED,
            producer=produced_by,
            detail=f"validation: {validated_or_error}",
        )
        return

    validated_or_error["status"] = "success"
    # Stash the extracted source text on the interpretation row when we
    # ran a pre-LLM binary extractor. Future cold-path workers (and the
    # embedding step below) can use it; recall surfaces it via
    # ``interpretation.extracted_text`` when full-payload mode is on.
    if extracted_text:
        validated_or_error["extracted_text"] = extracted_text
    write_interpretation(
        db,
        event=event,
        version=EXTRACTOR_SCHEMA_VERSION,
        produced_by=produced_by,
        extraction=validated_or_error,
    )

    # Enrich the FTS index with what the extractor learned about this
    # event. Without this step an FTS keyword search against a PDF or
    # image only matches the filename + mime metadata, NOT the body.
    # With it, a binary becomes first-class text-searchable via the
    # extractor's summary + salient_facts + the extracted body itself.
    _enrich_fts_after_extraction(
        db,
        content_hash=event.content_hash,
        payload=payload,
        extraction=validated_or_error,
        extracted_text=extracted_text,
    )

    log.info(
        "extractor.success",
        event_id=event_id,
        best_guess_kind=validated_or_error.get("best_guess_kind"),
        model=model,
        modality=modality,
    )
    pe.record(
        db,
        event_id=event_id,
        event_hash=event.content_hash,
        stage=pe.STAGE_EXTRACTION_COMPLETED,
        producer=produced_by,
        detail=f"modality={modality}",
    )

    # Generate + store the embedding for semantic recall. Failure here
    # is non-fatal — the substrate event is durable; only the vector
    # store doesn't get populated, and recall falls back to FTS for
    # this event.
    if semantic_recall_enabled:
        embedding_text = _embedding_text_for_event(event, validated_or_error)
        try:
            vector = embed_text(
                model=embedding_model,
                text=embedding_text,
                api_key=embedding_api_key,
            )
            _store_embedding(db, event.content_hash, vector)
            log.info(
                "extractor.embedding_stored",
                event_id=event_id,
                dim=len(vector),
                model=embedding_model,
            )
            pe.record(
                db,
                event_id=event_id,
                event_hash=event.content_hash,
                stage=pe.STAGE_EMBEDDING_STORED,
                producer=f"embedding:{embedding_model}",
                detail=f"dim={len(vector)}",
            )
            # Bind agent v0 — find prior semantically-similar events
            # and record the links. Soft-fail per binder.py.
            find_and_record_links(db, event=event, embedding=vector)
        except EmbeddingError as e:
            log.warning(
                "extractor.embedding_failed",
                event_id=event_id,
                error=str(e),
                model=embedding_model,
            )
            pe.record(
                db,
                event_id=event_id,
                event_hash=event.content_hash,
                stage=pe.STAGE_EMBEDDING_FAILED,
                status=pe.STATUS_FAILED,
                producer=f"embedding:{embedding_model}",
                detail=str(e)[:200],
            )


def _embedding_text_for_event(event: object, extraction: dict[str, object]) -> str:
    """Choose the text to embed for an event.

    We use a composed signal: the inline payload text (if any), the
    extracted text from any binary modality (PDF body, audio transcript),
    plus the extractor's summary and entity names. This gives the
    embedding raw content + binary-extract + the LLM's distilled view.
    """
    pieces: list[str] = []
    payload = getattr(event, "payload", {}) or {}
    if isinstance(payload, dict):
        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            pieces.append(text.strip())
        for key in ("context", "filename_hint", "action", "subject", "result"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                pieces.append(value.strip())
    # Binary-extracted text (PDF body, whisper transcript) was stored on
    # the interpretation row by _run_extraction — include it so semantic
    # recall finds the binary by its contents, not just its metadata.
    extracted = extraction.get("extracted_text")
    if isinstance(extracted, str) and extracted.strip():
        pieces.append(extracted.strip())
    summary = extraction.get("summary")
    if isinstance(summary, str) and summary.strip():
        pieces.append(summary.strip())
    entities = extraction.get("entities")
    if isinstance(entities, list):
        names = [e.get("name") if isinstance(e, dict) else None for e in entities]
        pieces.extend(n for n in names if isinstance(n, str) and n.strip())
    combined = "\n".join(pieces).strip()
    return combined or "(empty event)"


def _enrich_fts_after_extraction(
    db: Any,
    *,
    content_hash: str,
    payload: dict[str, Any],
    extraction: dict[str, Any],
    extracted_text: str | None,
) -> None:
    """Rewrite the events_fts row to include extractor-derived text.

    Two-stage indexing:
      1. write_event populated events_fts with the payload-derived
         searchable_text — for binaries that's only filename + mime +
         context (the bytes themselves aren't text).
      2. After the extractor lands, we REPLACE that row with the
         original searchable_text PLUS the extractor's distilled
         signal: summary, salient_facts, and (for PDFs / audio) the
         extracted body text itself.

    DELETE + INSERT rather than UPDATE so the FTS5 inverted-index
    rebuild stays uniform regardless of whether the row pre-existed.
    Lives inside the extractor's transaction window for atomicity.
    """
    from ..substrate.payload import derive_searchable_text

    pieces: list[str] = []
    base = derive_searchable_text(payload)
    if base:
        pieces.append(base)
    if extracted_text:
        pieces.append(extracted_text)
    summary = extraction.get("summary")
    if isinstance(summary, str) and summary.strip():
        pieces.append(summary.strip())
    salient = extraction.get("salient_facts")
    if isinstance(salient, list):
        pieces.extend(s for s in salient if isinstance(s, str) and s.strip())
    enriched = "\n".join(pieces).strip()
    if not enriched:
        return  # nothing to add; leave existing row in place

    with db:
        db.execute("DELETE FROM events_fts WHERE content_hash = ?", (content_hash,))
        db.execute(
            "INSERT INTO events_fts (content_hash, searchable_text) VALUES (?, ?)",
            (content_hash, enriched),
        )


def _store_embedding(db: object, content_hash: str, vector: list[float]) -> None:
    """Insert (or replace) the embedding row for ``content_hash``.

    sqlite-vec's vec0 virtual table does NOT honor ``INSERT OR REPLACE``
    semantics the way regular tables do — INSERT OR REPLACE against an
    existing primary key still fires the UNIQUE constraint. We do an
    explicit DELETE+INSERT instead so reprocess (which writes over a
    previous, possibly-stale embedding) always succeeds.

    The DELETE+INSERT is wrapped in its OWN transaction. Previously the two
    statements ran bare under ``isolation_level=''``, leaving the write
    uncommitted — its persistence then depended on a LATER, best-effort
    ``pipeline_events`` commit happening to flush it. If that follow-up write
    failed, the DELETE had already removed any prior vector and the new one
    was rolled back, silently losing the event's embedding. Committing here
    makes embedding persistence atomic and self-contained. (Race H1.)
    """
    serialized = serialize_vector(vector)
    with db:  # type: ignore[attr-defined]
        db.execute(  # type: ignore[attr-defined]
            "DELETE FROM events_vec WHERE content_hash = ?", (content_hash,)
        )
        db.execute(  # type: ignore[attr-defined]
            "INSERT INTO events_vec(content_hash, embedding) VALUES (?, ?)",
            (content_hash, serialized),
        )


def _validate_extraction(data: dict[str, object]) -> dict[str, object] | str:
    """Light validation. Returns the data dict on success, error string on failure.

    Intentionally permissive — refining the schema lives in the
    Interpretation layer per I3, not in writer-side enforcement that
    would reject otherwise-useful extractions.
    """
    required = ("best_guess_kind", "summary")
    for key in required:
        if key not in data:
            return f"missing required field: {key}"
        if not isinstance(data[key], str):
            return f"field {key} must be a string"
    return data


# Silence the noisy litellm logger in tests — we have our own structured logs.
logging.getLogger("LiteLLM").setLevel(logging.WARNING)
