"""Expectation-checker — cold-path worker that makes silent pipeline
failures visible (Phase 0.5 observability).

The extraction lifecycle already records every stage into
``pipeline_events`` (event.written → extraction.enqueued → …started →
…completed/…failed). What nothing yet does is *notice* when an event
enters that pipeline and never reaches a terminal stage. That is the
exact failure class Phase 0.5 targets: an ``event.written`` whose
extraction was enqueued but silently dropped — a swallowed executor
death, a rejected write, a process restart mid-flight — leaves no
terminal stage and no error row, so it is invisible to both the
extraction-retry worker (which keys off failed *interpretations*, not
missing ones) and to logs.

This worker runs every 15 minutes, computes four counts with two bounded
read-only queries, and appends one integer-only snapshot row to
``observability_snapshots``. ``/health`` reads only that latest row, so
the aggregates never run on Fly's ~30s probe. When violations exist it
emits one structured WARN carrying counts and at most
``MAX_VIOLATION_IDS_LOGGED`` event IDs — IDs and integers only, never
payloads, entity names, error strings, or paths.

What it deliberately does NOT do:

  - **No mutation, no remediation.** Detection only. Re-extraction is
    ``extraction_retry``'s job; entity-graph drain is
    ``scripts/checkup_entities.py``'s. This worker never writes to the
    substrate except the append-only snapshot row.
  - **No 503.** A backlog is a provider/workload condition, not an
    unhealthy machine. ``/health`` stays 200 on a backlog (a 503 would
    make Fly restart the machine and kill the cold-path scheduler
    mid-drain). Only a dead DB flips 503, unchanged.
  - **No embedding-failure expectations.** Embedding failure is
    non-fatal by design (FTS fallback) and already traced + logged, so
    it is not a silent failure.

Invariant fit: I2 (append-only — pure SELECTs plus one INSERT to an
append-only table), I3 (new read-side views over the unchanged
``pipeline_events`` / ``interpretations`` substrate; no migration).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from ..substrate import observability, pipeline_events
from .cold_path import ColdPathWorker
from .extraction_retry import MAX_EXTRACTION_RETRIES, TRANSIENT_ERROR_TYPES

if TYPE_CHECKING:
    import sqlite3

    from ..settings import Settings

log = structlog.get_logger(__name__)


EXTRACTION_GRACE_SECONDS = 1800
"""An ``event.written`` older than this with no terminal extraction stage
is counted as *stuck* (a genuine silent drop). Younger open events are
merely *pending* — extraction is synchronous and fast, so 30 minutes is
generous headroom over the warm path plus one hourly retry cycle."""

LOOKBACK_DAYS = 7
"""Every scan is bounded to the last week. Older gaps are a one-time
backfill concern (``admin reprocess``), not live monitoring, and leaving
them unbounded would grow the scan cost without operational value."""

MAX_VIOLATION_IDS_LOGGED = 10
"""The violation WARN carries at most this many event IDs (IDs only,
never content) so the log line stays bounded and leak-free."""

CHECK_INTERVAL_SECONDS = 900
"""15 minutes. The aggregate window-function query (query 2) is the same
shape as ``select_retry_candidates`` which already runs hourly in
production, so this cadence is a known-acceptable cost. Do NOT move it
into /health or shorten below 900s without measuring on a real vault."""


# ── query 1: stuck / pending extraction, from pipeline_events ────────────────
#
# ``event.written`` scoping automatically excludes agent-written events
# (consolidations, invalidations, mode switches) that legitimately never
# extract — only remember/observe handler writes emit event.written and
# schedule extraction.
_OPEN_EVENTS_CTE = """
WITH written AS (
    SELECT event_id, MIN(recorded_at) AS written_at
    FROM pipeline_events
    WHERE stage = :stage_written AND recorded_at >= :lookback_cutoff
    GROUP BY event_id
),
open_events AS (
    SELECT w.event_id, w.written_at
    FROM written w
    WHERE NOT EXISTS (
        SELECT 1 FROM pipeline_events t
        WHERE t.event_id = w.event_id
          AND t.stage IN (:stage_completed, :stage_failed)
    )
)
"""

_STUCK_AGGREGATE_SQL = (
    _OPEN_EVENTS_CTE
    + """
SELECT
    COALESCE(SUM(CASE WHEN written_at <= :grace_cutoff THEN 1 ELSE 0 END), 0)
        AS stuck_extractions,
    COALESCE(SUM(CASE WHEN written_at >  :grace_cutoff THEN 1 ELSE 0 END), 0)
        AS pending_extraction_backlog,
    MIN(CASE WHEN written_at <= :grace_cutoff THEN written_at END)
        AS oldest_stuck_written_at
FROM open_events
"""
)

_STUCK_SAMPLE_SQL = (
    _OPEN_EVENTS_CTE
    + """
SELECT event_id
FROM open_events
WHERE written_at <= :grace_cutoff
ORDER BY written_at ASC
LIMIT :sample_limit
"""
)


def _failed_aggregate_sql(transient_placeholders: str) -> str:
    """Query 2 — retry-exhausted + permanent failures, from interpretations.

    Same "latest extractor interpretation" idiom as
    ``select_retry_candidates`` (produced_at DESC, version DESC, id DESC).
    ``retry_exhausted`` = transiently-failing events extraction_retry has
    given up on (>= MAX attempts); ``permanent_failures`` = deterministic
    errors awaiting ``admin reprocess``.
    """
    return f"""
    WITH latest AS (
        SELECT event_hash, extraction,
               ROW_NUMBER() OVER (
                   PARTITION BY event_hash
                   ORDER BY produced_at DESC, version DESC, id DESC
               ) AS rn
        FROM interpretations
        WHERE produced_by LIKE 'extractor:%'
          AND produced_at >= :lookback_cutoff
    ),
    failed AS (
        SELECT json_extract(l.extraction, '$.error_type') AS error_type,
               (SELECT COUNT(*) FROM interpretations f
                WHERE f.event_hash = l.event_hash
                  AND f.produced_by LIKE 'extractor:%'
                  AND json_extract(f.extraction, '$.status') = 'failed') AS attempts
        FROM latest l
        WHERE l.rn = 1 AND json_extract(l.extraction, '$.status') = 'failed'
    )
    SELECT
        COALESCE(SUM(CASE WHEN error_type IN ({transient_placeholders})
                          AND attempts >= :max_retries THEN 1 ELSE 0 END), 0)
            AS retry_exhausted,
        COALESCE(SUM(CASE WHEN error_type NOT IN ({transient_placeholders})
                          THEN 1 ELSE 0 END), 0)
            AS permanent_failures
    FROM failed
    """


class ExpectationChecker(ColdPathWorker):
    """Detection-only observability worker. Counts silent pipeline
    failures and appends an integer-only snapshot. Cold path only."""

    name = "expectation_checker"
    interval_seconds = CHECK_INTERVAL_SECONDS

    def run(self, conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
        _ = settings  # scan is purely substrate-derived; no config needed

        now = datetime.now(UTC)
        lookback_cutoff = (now - timedelta(days=LOOKBACK_DAYS)).isoformat()
        grace_cutoff = (now - timedelta(seconds=EXTRACTION_GRACE_SECONDS)).isoformat()

        stage_params = {
            "stage_written": pipeline_events.STAGE_EVENT_WRITTEN,
            "stage_completed": pipeline_events.STAGE_EXTRACTION_COMPLETED,
            "stage_failed": pipeline_events.STAGE_EXTRACTION_FAILED,
            "lookback_cutoff": lookback_cutoff,
            "grace_cutoff": grace_cutoff,
        }

        stuck_row = conn.execute(_STUCK_AGGREGATE_SQL, stage_params).fetchone()
        stuck_extractions = int(stuck_row["stuck_extractions"])
        pending_extraction_backlog = int(stuck_row["pending_extraction_backlog"])
        oldest_stuck_written_at: str | None = stuck_row["oldest_stuck_written_at"]

        oldest_stuck_age_seconds: int | None = None
        if oldest_stuck_written_at is not None:
            try:
                oldest = datetime.fromisoformat(oldest_stuck_written_at)
            except ValueError:
                oldest = None
            if oldest is not None:
                oldest_stuck_age_seconds = int((now - oldest).total_seconds())

        # Query 2 — named placeholders reused twice (transient set appears
        # in both the retry-exhausted and permanent branches). sqlite3
        # binds each named param once regardless of how often it's cited.
        transient = sorted(TRANSIENT_ERROR_TYPES)
        transient_named = ",".join(f":t{i}" for i in range(len(transient)))
        failed_params: dict[str, Any] = {f"t{i}": v for i, v in enumerate(transient)}
        failed_params["max_retries"] = MAX_EXTRACTION_RETRIES
        # P2a: bound query 2's window scan to the same LOOKBACK_DAYS as query 1
        # (b68b665 added it only to query 1). Older interpretations are a
        # one-time backfill concern, not live monitoring, and leaving the window
        # unbounded grows the ROW_NUMBER scan without operational value.
        failed_params["lookback_cutoff"] = lookback_cutoff
        failed_row = conn.execute(_failed_aggregate_sql(transient_named), failed_params).fetchone()
        retry_exhausted = int(failed_row["retry_exhausted"])
        permanent_failures = int(failed_row["permanent_failures"])

        expectation_violations = stuck_extractions + retry_exhausted

        counters: dict[str, int | None] = {
            "stuck_extractions": stuck_extractions,
            "pending_extraction_backlog": pending_extraction_backlog,
            "retry_exhausted": retry_exhausted,
            "permanent_failures": permanent_failures,
            "expectation_violations": expectation_violations,
            "oldest_stuck_age_seconds": oldest_stuck_age_seconds,
            "lookback_days": LOOKBACK_DAYS,
        }

        # Always snapshot — even a clean cycle. The snapshot's age is
        # /health's liveness signal for the checker itself.
        observability.write_snapshot(conn, producer=self.name, counters=counters)

        if expectation_violations > 0:
            sample_ids = [
                row["event_id"]
                for row in conn.execute(
                    _STUCK_SAMPLE_SQL,
                    {**stage_params, "sample_limit": MAX_VIOLATION_IDS_LOGGED},
                ).fetchall()
            ]
            # Counts + IDs only. NEVER payloads, names, error strings, paths.
            log.warning(
                "expectation_checker.violations",
                stuck_extractions=stuck_extractions,
                pending_extraction_backlog=pending_extraction_backlog,
                retry_exhausted=retry_exhausted,
                permanent_failures=permanent_failures,
                expectation_violations=expectation_violations,
                sample_event_ids=sample_ids,
            )

        return dict(counters)
