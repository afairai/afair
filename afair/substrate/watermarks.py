"""Worker watermarks — cursorless-rescan relief for cold-path workers (P2a).

MUTABLE derived state, NOT substrate (Invariant I2 exception, exactly the
framing ``proposed_corrections`` / ``export_jobs`` already carry). A cold-path
worker records the high-water ULID cursor it has processed *through*, so it
stops re-scanning already-handled history every cycle. Deleting a row = that
worker re-scans from zero once, lossless. The table has NO append-only
triggers on purpose.

Never-skip contract
===================
A worker's ``through_id`` is a ULID such that EVERY candidate row whose ``id``
is ``<= through_id`` has already reached a terminal state (processed, or
permanently non-candidate). Selection adds ``id > through_id``. A worker
advances its watermark ONLY after a cycle that *fully drained* its candidate
set with zero failures — advancing to the max source-table id captured at
cycle START. A crash or a partial (budget-/limit-capped) cycle simply leaves
the watermark where it was, so the next cycle re-scans from there. Because the
existing ``NOT EXISTS`` / ``ROW_NUMBER`` correctness guards remain in every
worker, a re-scanned already-handled row is a no-op. Net effect: work may be
re-processed (fine), but it is **never skipped**.

Why the cursor is the ULID ``id``, not ``created_at``
-----------------------------------------------------
ULID ids are monotonic with INSERTION order. A backfill/eval write that
carries an explicit PAST ``created_at`` (events.py accepts ``created_at=``)
still gets a fresh, larger id — so it always sorts ABOVE any prior watermark
and can never be filtered out. A ``created_at`` cursor would silently skip
such a past-dated row. The through_created_at column is stored for human
inspection only; the guard and the selection filter both key on ``id``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3


# Worker keys — kept here so tests and workers share one source of truth.
WORKER_SALIENCE = "salience"
WORKER_TEMPORAL = "temporal"
WORKER_CANONICALIZER = "entity_canonicalizer"
WORKER_EXTRACTION_RETRY = "extraction_retry"


def read_watermark(conn: sqlite3.Connection, worker: str) -> tuple[str, str] | None:
    """Return ``(through_created_at, through_id)`` or ``None`` if the worker
    has no watermark yet (fresh vault, or the row was deleted for a re-scan)."""
    row = conn.execute(
        "SELECT through_created_at, through_id FROM worker_watermarks WHERE worker = ?",
        (worker,),
    ).fetchone()
    if row is None:
        return None
    return (row["through_created_at"], row["through_id"])


def read_watermark_id(conn: sqlite3.Connection, worker: str) -> str | None:
    """Convenience: just the ULID cursor (the value the selection filter uses)."""
    wm = read_watermark(conn, worker)
    return wm[1] if wm is not None else None


def write_watermark(
    conn: sqlite3.Connection,
    worker: str,
    *,
    through_created_at: str,
    through_id: str,
) -> None:
    """Upsert the high-water cursor. Advances only FORWARD.

    The ``WHERE excluded.through_id > worker_watermarks.through_id`` guard on
    the ``DO UPDATE`` makes a backwards (or equal) write a no-op — the cursor
    is monotonic and retry-safe. ULID string comparison is lexicographic =
    chronological, so this is a correct ordering.
    """
    with conn:
        conn.execute(
            """
            INSERT INTO worker_watermarks (
                worker, through_created_at, through_id, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(worker) DO UPDATE SET
                through_created_at = excluded.through_created_at,
                through_id         = excluded.through_id,
                updated_at         = excluded.updated_at
            WHERE excluded.through_id > worker_watermarks.through_id
            """,
            (worker, through_created_at, through_id, datetime.now(UTC).isoformat()),
        )


def frontier_events(conn: sqlite3.Connection) -> tuple[str, str] | None:
    """The ``(created_at, id)`` of the newest-inserted event, or ``None`` on an
    empty vault. This is the point a drained event-based worker (salience)
    advances its watermark to: at drain, every event with ``id <= this`` is a
    non-candidate, and any future write gets a larger id."""
    row = conn.execute("SELECT created_at, id FROM events ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return (row["created_at"], row["id"])


def frontier_interpretations(conn: sqlite3.Connection) -> tuple[str, str] | None:
    """The ``(produced_at, id)`` of the newest-inserted interpretation, or
    ``None`` when none exist. The advance point for interpretation-based
    workers (temporal, canonicalizer, extraction_retry). A global max is a
    safe upper bound: a worker's own marker/output rows written mid-cycle get
    larger ids, and any new extractor interpretation sorts above it."""
    row = conn.execute(
        "SELECT produced_at, id FROM interpretations ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return (row["produced_at"], row["id"])
