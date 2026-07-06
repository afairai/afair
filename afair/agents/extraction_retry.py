"""Extraction-retry — cold-path worker that re-runs transient extraction failures.

The warm-path Extractor records failures as ``status: failed``
interpretation rows (option (b) — see extractor.py). Before this worker
existed, nothing ever re-attempted them automatically: an
``llm_timeout`` on a large document left the event permanently without
entities/relations/summary and invisible to entity-scoped recall, unless
the operator happened to run ``python -m afair.admin reprocess`` by hand.

This worker closes that silent gap for TRANSIENT failures only:

  - **Transient** (retried): ``llm_timeout``, ``llm_rate_limit`` — the
    provider was slow or throttling; the same call can succeed later.
  - **Deterministic** (never retried): ``pdf_extraction_error``,
    ``image_payload_error``, ``text_large_read_error``,
    ``llm_response_error`` / validation errors, ``llm_auth_error`` —
    re-running the identical input would just fail again (auth errors
    need an operator fix, not a retry loop).

Invariant fit:

  - **I2 append-only.** A retry never mutates the failed row. It re-runs
    the normal extraction path, which appends a NEW interpretation row;
    ``write_interpretation`` disambiguates via the ``#retryN`` producer
    suffix and ``read_latest_interpretation`` (ordered ``produced_at
    DESC``) automatically prefers the newest success. The failure stays
    as audit trail, exactly as I3 intends.
  - **Bounded.** The attempt count is derived honestly from the number
    of failed extractor rows for the event (no mutable counter). Once
    ``MAX_EXTRACTION_RETRIES`` failed attempts exist, the event is never
    selected again. Each cycle retries at most ``MAX_RETRIES_PER_RUN``
    events, so a provider outage can't pin the scheduler or blow the
    LLM budget.

Never-attempted events are intentionally NOT this worker's job: only
``remember``/``observe`` handler writes schedule extraction, and many
agent-written events (consolidations, invalidations, mode switches)
correctly have no extractor interpretation at all. Selecting on "latest
extractor interpretation is a transient failure" scopes the worker to
exactly the events the warm path already chose to extract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from ..substrate import watermarks
from .cold_path import ColdPathWorker
from .interpretation import read_latest_interpretation

if TYPE_CHECKING:
    import sqlite3

    from ..settings import Settings

log = structlog.get_logger(__name__)


TRANSIENT_ERROR_TYPES: frozenset[str] = frozenset({"llm_timeout", "llm_rate_limit"})
"""Failed-extraction ``error_type`` values worth retrying automatically.

Deliberately conservative: only errors where the input is fine and the
provider hiccupped. The generic ``llm_error`` bucket is excluded because
it mixes transient 5xx with deterministic request errors (e.g. context
length) — those go through ``admin reprocess`` after a fix instead.
"""

MAX_EXTRACTION_RETRIES = 3
"""Maximum total failed extraction attempts per event (initial + retries).

Once an event has this many ``status: failed`` extractor rows, the
worker stops selecting it — the failure is then treated as persistent
and left for the manual ``admin reprocess`` path.
"""

MAX_RETRIES_PER_RUN = 5
"""Per-cycle cap on retried events. Keeps a single run bounded in both
wall time (the extraction LLM call is synchronous here) and LLM spend;
any backlog beyond the cap is picked up on subsequent cycles."""


def select_retry_candidates(
    conn: sqlite3.Connection, *, limit: int = MAX_RETRIES_PER_RUN, wm_id: str | None = None
) -> list[tuple[str, str]]:
    """Events whose LATEST extractor interpretation is a transient failure.

    Returns ``(event_id, event_hash)`` pairs, oldest interpretation first,
    capped at ``limit``. An event is a candidate iff:

      1. its most recent ``extractor:%`` interpretation (same
         ``produced_at DESC, version DESC`` order recall uses) has
         ``status: failed`` with an ``error_type`` in
         :data:`TRANSIENT_ERROR_TYPES`, and
      2. fewer than :data:`MAX_EXTRACTION_RETRIES` failed extractor rows
         exist for it in total (the honest attempt count — each failed
         retry appends another row, so the count can only grow toward
         the cap, never loop).

    Events with no extractor interpretation at all are never selected
    (see module docstring), and a success as the latest row ends the
    retry stream naturally.

    ``wm_id`` (P2a): when set, the ``latest`` window only scans extractor
    interpretations above the worker's watermark id. This is skip-safe: a
    retry ALWAYS appends a new interpretation (a larger id), so a hash still
    failing after a drained cycle re-enters via that new row; a hash whose
    latest interpretation is at/below the cursor was already terminal.
    """
    transient = sorted(TRANSIENT_ERROR_TYPES)
    transient_named = ",".join(f":t{i}" for i in range(len(transient)))
    params: dict[str, Any] = {f"t{i}": v for i, v in enumerate(transient)}
    params["max_retries"] = MAX_EXTRACTION_RETRIES
    params["limit"] = limit
    params["wm_id"] = wm_id
    rows = conn.execute(
        f"""
        WITH latest AS (
            SELECT id, event_id, event_hash, extraction,
                   ROW_NUMBER() OVER (
                       PARTITION BY event_hash
                       ORDER BY produced_at DESC, version DESC, id DESC
                   ) AS rn
            FROM interpretations
            WHERE produced_by LIKE 'extractor:%'
              AND (:wm_id IS NULL OR id > :wm_id)
        )
        SELECT l.event_id, l.event_hash
        FROM latest l
        WHERE l.rn = 1
          AND json_extract(l.extraction, '$.status') = 'failed'
          AND json_extract(l.extraction, '$.error_type') IN ({transient_named})
          AND (
              SELECT COUNT(*) FROM interpretations f
              WHERE f.event_hash = l.event_hash
                AND f.produced_by LIKE 'extractor:%'
                AND json_extract(f.extraction, '$.status') = 'failed'
          ) < :max_retries
        ORDER BY l.id ASC
        LIMIT :limit
        """,
        params,
    ).fetchall()
    return [(row["event_id"], row["event_hash"]) for row in rows]


class ExtractionRetryWorker(ColdPathWorker):
    """Bounded re-extraction of transiently-failed events. Cold path only."""

    name = "extraction_retry"
    interval_seconds = 3600  # hourly; steady-state candidate count is ~0

    def run(self, conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
        # Lazy import — extractor pulls in the mcp.context module chain;
        # keeping it out of module import time matches extractor.py's own
        # circular-import discipline.
        from .extractor import extract_sync

        _ = settings  # extraction config comes from the live ServerContext

        # Watermark (P2a): frontier captured BEFORE selection AND before
        # extract_sync appends any new interpretation. Cursor keys on the
        # interpretation id.
        frontier = watermarks.frontier_interpretations(conn)
        wm_id = watermarks.read_watermark_id(conn, watermarks.WORKER_EXTRACTION_RETRY)

        candidates = select_retry_candidates(conn, wm_id=wm_id)
        stats: dict[str, Any] = {
            "candidates": len(candidates),
            "succeeded": 0,
            "still_failing": 0,
        }
        # Count only the except-path failures separately: those append NO new
        # interpretation row, so the candidate's latest interp stays at/below
        # the frontier and would be SKIPPED if we advanced past it. A normal
        # "still failing" retry appended a fresh (larger-id) failed row and is
        # safely re-selected next cycle, so it does NOT block the advance.
        unwritten_failures = 0
        for event_id, event_hash in candidates:
            log.info("extraction_retry.retrying", event_id=event_id)
            try:
                extract_sync(event_id)
            except Exception as e:
                # extract_sync records LLM failures itself as failed rows;
                # anything reaching here is unexpected (e.g. missing
                # context) and appended NO row. Count it and keep going —
                # the cap still bounds future selection because no row means
                # no growth either.
                log.warning("extraction_retry.error", event_id=event_id, error=str(e))
                stats["still_failing"] += 1
                unwritten_failures += 1
                continue
            latest = read_latest_interpretation(conn, event_hash)
            if latest is not None and latest.extraction.get("status") != "failed":
                stats["succeeded"] += 1
                log.info("extraction_retry.recovered", event_id=event_id)
            else:
                stats["still_failing"] += 1

        # Drain-advance (P2a): only when fewer than the cap were selected (no
        # backlog) AND every candidate got a new interpretation row (no
        # except-path leaving one at/below the frontier). Then every extractor
        # interpretation with id <= frontier is terminal → skip-safe.
        if (
            frontier is not None
            and len(candidates) < MAX_RETRIES_PER_RUN
            and unwritten_failures == 0
        ):
            watermarks.write_watermark(
                conn,
                watermarks.WORKER_EXTRACTION_RETRY,
                through_created_at=frontier[0],
                through_id=frontier[1],
            )
        return stats
