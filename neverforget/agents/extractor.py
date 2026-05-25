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
from .interpretation import (
    write_failed_interpretation,
    write_interpretation,
)
from .llm import LLMError, LLMResponseError, call_json
from .prompts import (
    EXTRACTOR_SCHEMA_VERSION,
    EXTRACTOR_SYSTEM_PROMPT,
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


def schedule_extraction(event_id: str) -> None:
    """Fire-and-forget the extraction for ``event_id``. Returns immediately.

    Reads the active ServerContext synchronously to capture the model name,
    vault path, and API keys; then dispatches the actual work to the
    background pool. The background thread opens its own SQLite connection,
    so this is safe even if the main connection is busy.
    """
    from ..mcp.context import get_context  # lazy — breaks circular import

    ctx = get_context()
    api_key = (
        ctx.anthropic_api_key.get_secret_value() if ctx.anthropic_api_key is not None else None
    )
    # OpenAI key fallback when the model selects an OpenAI model.
    if ctx.extractor_model.startswith("openai/") and ctx.openai_api_key is not None:
        api_key = ctx.openai_api_key.get_secret_value()
    if ctx.extractor_model.startswith("gemini/") and ctx.gemini_api_key is not None:
        api_key = ctx.gemini_api_key.get_secret_value()

    _EXECUTOR.submit(
        _run_extraction,
        event_id=event_id,
        vault_dir=ctx.vault_dir,
        model=ctx.extractor_model,
        api_key=api_key,
    )


def extract_sync(event_id: str) -> None:
    """Synchronous extraction — for tests and debugging.

    Same code path as the fire-and-forget variant but blocks until done.
    """
    from ..mcp.context import get_context  # lazy — breaks circular import

    ctx = get_context()
    api_key = (
        ctx.anthropic_api_key.get_secret_value() if ctx.anthropic_api_key is not None else None
    )
    if ctx.extractor_model.startswith("openai/") and ctx.openai_api_key is not None:
        api_key = ctx.openai_api_key.get_secret_value()
    if ctx.extractor_model.startswith("gemini/") and ctx.gemini_api_key is not None:
        api_key = ctx.gemini_api_key.get_secret_value()

    _run_extraction(
        event_id=event_id,
        vault_dir=ctx.vault_dir,
        model=ctx.extractor_model,
        api_key=api_key,
    )


def _run_extraction(
    *,
    event_id: str,
    vault_dir: Path,
    model: str,
    api_key: str | None,
) -> None:
    """The actual extraction work. Opens its own DB connection per thread."""
    db = open_db(vault_dir)
    try:
        event = read_event_by_id(db, event_id)
        if event is None:
            log.warning("extractor.event_missing", event_id=event_id)
            return

        produced_by = f"extractor:{model}"

        try:
            result = call_json(
                model=model,
                system=EXTRACTOR_SYSTEM_PROMPT,
                user=build_user_message(event),
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
    finally:
        db.close()


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
