"""Mutable operational queue and cross-process leases, never user memory."""

from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import structlog

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator

LEASE_SECONDS = 900
DEFER_SECONDS = 300
MAX_JOB_FAILURES = 3
MAX_BACKOFF_SECONDS = 3600
INITIAL_GRACE_SECONDS = 300
log = structlog.get_logger(__name__)


class ExtractionDeferred(Exception):
    """Another process owns this event, or its retry backoff has not elapsed."""


def initial_available_at(created_at: str, *, grace_seconds: int = INITIAL_GRACE_SECONDS) -> float:
    """Legacy malformed dates cannot block a whole discovery batch."""
    try:
        created = datetime.fromisoformat(created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return created.timestamp() + grace_seconds
    except (ValueError, TypeError, OverflowError, OSError):
        # Unknown age: give it the same grace as newly committed intake.
        return time.time() + grace_seconds


def enqueue_initial(conn: sqlite3.Connection, event_id: str) -> None:
    row = conn.execute("SELECT created_at FROM events WHERE id=?", (event_id,)).fetchone()
    if row is None:
        return
    with conn:
        conn.execute(
            """INSERT INTO extraction_jobs(event_id,available_at,priority) VALUES (?,?,1)
            ON CONFLICT(event_id) DO UPDATE SET priority=MAX(extraction_jobs.priority,1)""",
            (event_id, initial_available_at(row["created_at"])),
        )


@contextmanager
def extraction_lease(
    conn: sqlite3.Connection,
    event_id: str,
    *,
    purpose: str = "initial",
    warm_start: bool = False,
) -> Iterator[None]:
    from .usage import BackgroundBudgetExceeded

    token = uuid4().hex
    now = time.time()
    with conn:
        conn.execute("INSERT OR IGNORE INTO extraction_jobs(event_id) VALUES (?)", (event_id,))
        claimed = conn.execute(
            """UPDATE extraction_jobs SET lease_token=?,lease_until=?,purpose=?
            WHERE event_id=? AND terminal=0 AND lease_until < ?
            AND (available_at <= ? OR (? AND failures=0 AND last_error_type IS NULL))
            RETURNING failures""",
            (token, now + LEASE_SECONDS, purpose, event_id, now, now, warm_start),
        ).fetchone()
    if claimed is None:
        raise ExtractionDeferred("extraction is leased, terminal or deferred")
    primary: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            if primary is not None and conn.in_transaction:
                conn.rollback()
            attempted = conn.execute(
                "SELECT 1 FROM interpretations WHERE event_id=? "
                "AND produced_by LIKE 'extractor:%' LIMIT 1",
                (event_id,),
            ).fetchone()
            with conn:
                if attempted is not None and primary is None:
                    conn.execute(
                        "DELETE FROM extraction_jobs WHERE event_id=? AND lease_token=?",
                        (event_id, token),
                    )
                else:
                    # Budget refusals are durable deferrals, not failed attempts.
                    # Unexpected post-provider/local failures have their own finite
                    # operational cap, independent of extraction interpretation rows.
                    count_failure = primary is None or (
                        isinstance(primary, Exception)
                        and not isinstance(primary, (BackgroundBudgetExceeded, ExtractionDeferred))
                    )
                    failures = claimed["failures"] + int(count_failure)
                    delay = min(MAX_BACKOFF_SECONDS, DEFER_SECONDS * 2 ** min(failures, 4))
                    conn.execute(
                        """UPDATE extraction_jobs SET lease_until=0,lease_token=NULL,
                        available_at=?, failures=?, terminal=?, last_error_type=?
                        WHERE event_id=? AND lease_token=?""",
                        (
                            time.time() + delay,
                            failures,
                            int(failures >= MAX_JOB_FAILURES),
                            type(primary).__name__ if primary else "missing_interpretation",
                            event_id,
                            token,
                        ),
                    )
            if (attempted is None or primary is not None) and failures >= MAX_JOB_FAILURES:
                log.warning(
                    "extraction.job_exhausted",
                    event_id=event_id,
                    failures=failures,
                    error_type=type(primary).__name__ if primary else "missing_interpretation",
                )
        except Exception as cleanup_error:
            # A cleanup failure must not disguise the provider/budget exception.
            # Its token lease remains bounded and may be recovered after expiry.
            log.warning(
                "extraction.lease_release_failed",
                event_id=event_id,
                error_type=type(cleanup_error).__name__,
                primary_error=type(primary).__name__,
            )
            if primary is None:
                raise
