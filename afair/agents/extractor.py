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


def schedule_extraction(event_id: str) -> None:
    """Fire-and-forget the extraction for ``event_id``. Returns immediately.

    Reads the active ServerContext synchronously to capture the model name,
    vault path, and API keys; then dispatches the actual work to the
    background pool. The background thread opens its own SQLite connection,
    so this is safe even if the main connection is busy.
    """
    from ..mcp.context import get_context  # lazy — breaks circular import

    ctx = get_context()
    try:
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
    except RuntimeError as exc:
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
) -> None:
    """The actual extraction work. Reuses a thread-local DB connection."""
    db = _conn_for_extractor_thread(vault_dir, embedding_dim)
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
        # Connection lives on the thread for the worker's lifetime — see
        # _conn_for_extractor_thread for the rationale. No close here.
        pass


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
    """Insert (or replace) the embedding row for ``content_hash``.

    sqlite-vec's vec0 virtual table does NOT honor ``INSERT OR REPLACE``
    semantics the way regular tables do — INSERT OR REPLACE against an
    existing primary key still fires the UNIQUE constraint. We do an
    explicit DELETE+INSERT instead so reprocess (which writes over a
    previous, possibly-stale embedding) always succeeds.
    """
    serialized = serialize_vector(vector)
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
