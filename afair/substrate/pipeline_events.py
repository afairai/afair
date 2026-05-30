"""End-to-end pipeline-lifecycle tracing.

Records one row per stage in an event's journey from substrate write
through extraction → embedding → entity canonicalization → consolidation.
The table is append-only (Invariant I2 triggers); readers compose the
timeline with ``ORDER BY recorded_at``.

Use this instead of grepping logs when answering:
  * "Where did event 01KSX... get stuck?"
  * "Did this PDF's extractor actually run?"
  * "How long did vision-extraction take for screenshots last week?"
  * "Which events haven't been consolidated yet?"

Helpers here are intentionally tolerant — emit failures must NEVER
break the calling code path. A pipeline_event write that itself fails
is logged and swallowed; the underlying lifecycle work (the extraction,
the embedding, etc.) proceeds.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from ulid import ULID

if TYPE_CHECKING:
    import sqlite3


log = structlog.get_logger(__name__)


# Lifecycle stages — short codes so they fit nicely in `stage` filter
# queries. Kept as module-level strings (not an enum) so adding a new
# stage is one line, no migration.
STAGE_EVENT_WRITTEN = "event.written"
STAGE_EXTRACTION_ENQUEUED = "extraction.enqueued"
STAGE_EXTRACTION_STARTED = "extraction.started"
STAGE_EXTRACTION_COMPLETED = "extraction.completed"
STAGE_EXTRACTION_FAILED = "extraction.failed"
STAGE_EMBEDDING_STORED = "embedding.stored"
STAGE_EMBEDDING_FAILED = "embedding.failed"
STAGE_BINDER_LINKED = "binder.linked"
STAGE_CANONICALIZER_PROCESSED = "canonicalizer.processed"
STAGE_CONSOLIDATOR_INCLUDED = "consolidator.included"
STAGE_CONFLICT_RESOLVER_JUDGED = "conflict_resolver.judged"

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


def record(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    stage: str,
    status: str = STATUS_OK,
    event_hash: str | None = None,
    producer: str | None = None,
    detail: str | None = None,
) -> None:
    """Append one pipeline_event row. Best-effort — failures are logged
    and swallowed so this never breaks the calling lifecycle path.

    ``producer`` identifies what wrote the row (model string for
    LLM-driven steps, "schedule_extraction" for the orchestrator, etc.).
    ``detail`` is free-text for human diagnosis — keep it short.
    """
    try:
        # ``with conn:`` commits the implicit transaction Python's
        # sqlite3 driver opens for INSERT statements. Without it the
        # write lock stays held until the connection itself commits,
        # which can block concurrent open_db() callers (test
        # discovered this against the extractor's per-thread
        # connection).
        with conn:
            conn.execute(
                """
                INSERT INTO pipeline_events (
                    id, event_id, event_hash, stage, status,
                    recorded_at, producer, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(ULID()),
                    event_id,
                    event_hash,
                    stage,
                    status,
                    datetime.now(UTC).isoformat(),
                    producer,
                    detail[:500] if detail else None,  # cap free-text length
                ),
            )
    except Exception as e:
        # Swallow — the substrate row is already durable; tracing is
        # advisory, not load-bearing.
        log.warning(
            "pipeline_events.write_failed",
            event_id=event_id,
            stage=stage,
            error=str(e),
        )


def record_safe(
    conn_factory: object,
    *,
    event_id: str,
    stage: str,
    status: str = STATUS_OK,
    event_hash: str | None = None,
    producer: str | None = None,
    detail: str | None = None,
) -> None:
    """Variant for call sites that don't already hold a DB connection.

    ``conn_factory`` is anything with an ``execute`` callable, OR a
    callable that returns a connection. Falls back to a no-op if the
    factory can't be resolved (e.g., during test teardown when the
    server context is torn down).
    """
    conn = None
    with contextlib.suppress(Exception):
        if callable(conn_factory):
            conn = conn_factory()
        elif hasattr(conn_factory, "execute"):
            conn = conn_factory
    if conn is None:
        return
    record(
        conn,
        event_id=event_id,
        stage=stage,
        status=status,
        event_hash=event_hash,
        producer=producer,
        detail=detail,
    )
