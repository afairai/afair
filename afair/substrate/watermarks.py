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
set with zero failures — advancing to a LAGGED frontier (see below). A crash or
a partial (budget-/limit-capped) cycle simply leaves the watermark where it
was, so the next cycle re-scans from there. Because the existing ``NOT EXISTS``
/ ``ROW_NUMBER`` correctness guards remain in every worker, a re-scanned
already-handled row is a no-op. Net effect: work may be re-processed (fine), but
it is **never skipped**.

Why the cursor is the ULID ``id``, and why the frontier LAGS
------------------------------------------------------------
The cursor is the ULID ``id``, not ``created_at``: a backfill/eval write that
carries an explicit PAST ``created_at`` (events.py accepts ``created_at=``)
still gets a fresh id, so a ``created_at`` cursor would silently skip it. The
``through_created_at`` column is stored for inspection only; the guard and the
selection filter both key on ``id``.

But ULID ids are NOT reliably monotonic with COMMIT order in this server's
concurrent-writer regime. Ids are minted BEFORE the write lock — ``events.py``
mints ``event_id`` then does ``canonical_json`` + ``derive_searchable_text``
(tens of ms on a big paste) before ``with conn: INSERT``; ``interpretation.py``
is the same. With the 4-thread extractor pool, up to 12 MCP tool threads, and
an unfair ``busy_timeout`` queue, a writer that minted an id FIRST can commit
LAST (``entities.py`` already notes "plain ULIDs aren't monotonic within a
millisecond"). So a naive ``frontier = MAX(id)`` could advance the cursor past
an id that a slower writer, which minted an even SMALLER id, has not yet
committed — stranding that row below the cursor forever (the ``id > wm`` filter
excludes it from every future scan, and the NOT EXISTS backstops never fire
because the worker never sees it).

The fix: the advanceable frontier EXCLUDES the most recent
``FRONTIER_LAG_SECONDS`` (by ULID timestamp). The cursor can only advance to an
id whose millisecond timestamp is strictly older than ``now - lag``. Any
in-flight id minted within that window sorts ABOVE the frontier, so it can
never be stranded; once it is both older than the lag AND committed, the next
cycle includes it. The lag must exceed the worst mint→commit gap
(``busy_timeout`` 5s + serialization); 60s is comfortably safe and well below
every cold-path interval, so the only cost is that the trailing ~60s of history
is always re-scanned (idempotent via the correctness guards).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ulid import ULID

if TYPE_CHECKING:
    import sqlite3


FRONTIER_LAG_SECONDS = 60
"""The advanceable frontier excludes ids minted within this many seconds (by
ULID timestamp), so a concurrent writer's pre-lock-minted id can't be stranded
below an advanced cursor. Must exceed the worst mint→commit gap (busy_timeout
5s + serialization). 60s is safe and below every cold-path interval."""


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


def _lag_threshold_id() -> str:
    """The minimum ULID for the timestamp ``now - FRONTIER_LAG_SECONDS`` — an
    all-zero-randomness ULID whose millisecond timestamp is the lag horizon.

    Because the 48-bit timestamp dominates lexicographic order and the
    randomness is zero, ``id < threshold`` holds iff ``id``'s timestamp is
    strictly older than the horizon. Frontier queries add ``id < threshold`` so
    the advanceable cursor never reaches an id minted within the lag window."""
    horizon = datetime.now(UTC) - timedelta(seconds=FRONTIER_LAG_SECONDS)
    ms = int(horizon.timestamp() * 1000)
    return str(ULID.from_bytes(ms.to_bytes(6, "big") + b"\x00" * 10))


def frontier_events(conn: sqlite3.Connection) -> tuple[str, str] | None:
    """The ``(created_at, id)`` of the newest LAGGED event (older than
    ``FRONTIER_LAG_SECONDS`` by ULID timestamp), or ``None`` when no such event
    exists. This is the point a drained event-based worker (salience) advances
    its watermark to: at drain, every event with ``id <= this`` is a
    non-candidate, and — because the frontier lags — any concurrently-committing
    pre-minted id sorts above it and can't be stranded (see module docstring)."""
    row = conn.execute(
        "SELECT created_at, id FROM events WHERE id < ? ORDER BY id DESC LIMIT 1",
        (_lag_threshold_id(),),
    ).fetchone()
    if row is None:
        return None
    return (row["created_at"], row["id"])


def frontier_interpretations(conn: sqlite3.Connection) -> tuple[str, str] | None:
    """The ``(produced_at, id)`` of the newest LAGGED interpretation (older than
    ``FRONTIER_LAG_SECONDS`` by ULID timestamp), or ``None`` when none exist.
    The advance point for interpretation-based workers (temporal, canonicalizer,
    extraction_retry). The lag keeps a slower concurrent writer's pre-minted id
    above the advanceable cursor; a worker's own marker/output rows written this
    cycle are within the lag window and so never bump the cursor prematurely."""
    row = conn.execute(
        "SELECT produced_at, id FROM interpretations WHERE id < ? ORDER BY id DESC LIMIT 1",
        (_lag_threshold_id(),),
    ).fetchone()
    if row is None:
        return None
    return (row["produced_at"], row["id"])
