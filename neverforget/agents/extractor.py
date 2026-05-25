"""The warm-path Extractor agent.

Triggered after each successful ``remember`` or ``observe`` write. Runs the
LLM call on a background thread so the user-facing tool call returns
immediately. Produces one Interpretation row per event — success or failure
(option (b)): failures are themselves stored, so retry and diagnosis are
observable without touching the substrate.
"""

from __future__ import annotations

import atexit
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import structlog

from ..substrate import open_db
from ..substrate.events import read_event_by_id
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


def schedule_extraction(event_id: str) -> None:
    """Fire-and-forget the extraction for ``event_id``. Returns immediately.

    Reads the active ServerContext synchronously to capture the model name,
    vault path, and API keys; then dispatches the actual work to the
    background pool. The background thread opens its own SQLite connection,
    so this is safe even if the main connection is busy.
    """
    from ..mcp.context import get_context  # lazy — breaks circular import

    ctx = get_context()
    _EXECUTOR.submit(
        _run_extraction,
        event_id=event_id,
        vault_dir=ctx.vault_dir,
        model=ctx.extractor_model,
        api_key=_api_key_for(ctx.extractor_model, ctx),
        embedding_model=ctx.embedding_model,
        embedding_api_key=_api_key_for(ctx.embedding_model, ctx),
        embedding_dim=ctx.embedding_dim,
        semantic_recall_enabled=ctx.semantic_recall_enabled,
    )


def extract_sync(event_id: str) -> None:
    """Synchronous extraction — for tests and debugging.

    Same code path as the fire-and-forget variant but blocks until done.
    """
    from ..mcp.context import get_context  # lazy — breaks circular import

    ctx = get_context()
    _run_extraction(
        event_id=event_id,
        vault_dir=ctx.vault_dir,
        model=ctx.extractor_model,
        api_key=_api_key_for(ctx.extractor_model, ctx),
        embedding_model=ctx.embedding_model,
        embedding_api_key=_api_key_for(ctx.embedding_model, ctx),
        embedding_dim=ctx.embedding_dim,
        semantic_recall_enabled=ctx.semantic_recall_enabled,
    )


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
) -> None:
    """The actual extraction work. Opens its own DB connection per thread."""
    db = open_db(vault_dir, embedding_dim=embedding_dim)
    try:
        event = read_event_by_id(db, event_id)
        if event is None:
            log.warning("extractor.event_missing", event_id=event_id)
            return

        produced_by = f"extractor:{model}"

        try:
            result = call_tool(
                model=model,
                system=EXTRACTOR_SYSTEM_PROMPT,
                user=build_user_message(event),
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

        # Validate the extraction has at least the mandatory top-level keys.
        validated_or_error = _validate_extraction(result.data)
        if isinstance(validated_or_error, str):
            log.warning(
                "extractor.validation_error",
                event_id=event_id,
                error=validated_or_error,
                model=model,
            )
            write_failed_interpretation(
                db,
                event=event,
                version=EXTRACTOR_SCHEMA_VERSION,
                produced_by=produced_by,
                error_type=LLMResponseError.error_type,
                error_message=validated_or_error,
            )
            return

        validated_or_error["status"] = "success"
        write_interpretation(
            db,
            event=event,
            version=EXTRACTOR_SCHEMA_VERSION,
            produced_by=produced_by,
            extraction=validated_or_error,
        )
        log.info(
            "extractor.success",
            event_id=event_id,
            best_guess_kind=validated_or_error.get("best_guess_kind"),
            model=model,
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
    finally:
        db.close()


def _embedding_text_for_event(event: object, extraction: dict[str, object]) -> str:
    """Choose the text to embed for an event.

    We use a composed signal: the inline payload text (if any) plus the
    extractor's summary plus the entity names. This gives the embedding
    both raw content and the LLM's distilled understanding.
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
    summary = extraction.get("summary")
    if isinstance(summary, str) and summary.strip():
        pieces.append(summary.strip())
    entities = extraction.get("entities")
    if isinstance(entities, list):
        names = [e.get("name") if isinstance(e, dict) else None for e in entities]
        pieces.extend(n for n in names if isinstance(n, str) and n.strip())
    combined = "\n".join(pieces).strip()
    return combined or "(empty event)"


def _store_embedding(db: object, content_hash: str, vector: list[float]) -> None:
    """Insert (or replace) the embedding row for ``content_hash``."""
    serialized = serialize_vector(vector)
    # vec0 virtual table accepts INSERT OR REPLACE for upsert semantics.
    db.execute(  # type: ignore[attr-defined]
        "INSERT OR REPLACE INTO events_vec(content_hash, embedding) VALUES (?, ?)",
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


# Test helper — flush pending background extractions in unit tests.
def _wait_for_pending(timeout: float = 10.0) -> None:
    """Wait for all currently-enqueued extractions to complete.

    Not part of the public surface; used only in tests.
    """
    # ThreadPoolExecutor doesn't expose "wait for current queue" directly.
    # We submit a no-op and wait on its future — by the time it runs, all
    # earlier-submitted tasks have completed in our single thread.
    # For max_workers>1, this is approximate; tests use a single worker.
    _EXECUTOR.submit(lambda: None).result(timeout=timeout)


# Silence the noisy litellm logger in tests — we have our own structured logs.
logging.getLogger("LiteLLM").setLevel(logging.WARNING)
