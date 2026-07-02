"""Observability snapshots — the expectation-checker's counters overlay.

The cold-path :class:`~afair.agents.expectation_checker.ExpectationChecker`
appends one row per cycle summarising pipeline health as pure integer
counts (stuck extractions, retry-exhausted events, permanent failures,
…). ``/health`` reads only the latest row — a single indexed ``LIMIT 1``
lookup — so the window-function aggregates that produce the counts never
run on Fly's ~30s health probe.

Invariant fit:

  - **I2 append-only.** The table has the same RAISE(ABORT) no-UPDATE /
    no-DELETE triggers as ``pipeline_events`` (schema.py). :func:`write_snapshot`
    only ever INSERTs.
  - **Counts only.** :func:`write_snapshot` rejects any counter value that
    is not ``int | None`` (raises ``ValueError``). This is the code-level
    guarantee that no content, entity name, error string, or filesystem
    path can ever reach the unauthenticated ``/health`` body through a
    snapshot.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ulid import ULID

if TYPE_CHECKING:
    import sqlite3


def write_snapshot(
    conn: sqlite3.Connection,
    *,
    producer: str,
    counters: dict[str, int | None],
) -> None:
    """Append one observability snapshot row.

    Every value in ``counters`` MUST be ``int`` or ``None`` — anything
    else (a string, a float, a dict) raises ``ValueError`` before the
    write. This is the load-bearing guarantee that only counts/ages/nulls
    reach ``/health``; strings (which could carry content or the vault
    path) are impossible by construction.

    Unlike ``pipeline_events.record`` this is NOT best-effort: the checker
    already runs inside the scheduler's per-worker try/except
    (cold_path.py), so a snapshot-write failure is surfaced there rather
    than silently swallowed here.
    """
    for key, value in counters.items():
        # bool is an int subclass and is an acceptable scalar; only
        # non-int, non-None values (strings, floats, containers) are
        # content-shaped and therefore forbidden.
        if value is not None and not isinstance(value, int):
            raise ValueError(
                f"observability counter {key!r} must be int | None, got {type(value).__name__}"
            )
    with conn:
        conn.execute(
            """
            INSERT INTO observability_snapshots (id, recorded_at, producer, counters)
            VALUES (?, ?, ?, ?)
            """,
            (
                str(ULID()),
                datetime.now(UTC).isoformat(),
                producer,
                json.dumps(counters),
            ),
        )


def read_latest_snapshot(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The most recent snapshot, or ``None`` if the checker hasn't run yet.

    Returns ``{"recorded_at": str, "counters": dict}``. A malformed
    ``counters`` JSON (should never happen — the writer controls it)
    degrades to ``None`` rather than raising, so ``/health`` can never be
    broken by a bad snapshot row.
    """
    row = conn.execute(
        """
        SELECT recorded_at, counters
        FROM observability_snapshots
        ORDER BY recorded_at DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    try:
        counters = json.loads(row["counters"])
    except (ValueError, TypeError):
        return None
    if not isinstance(counters, dict):
        return None
    return {"recorded_at": row["recorded_at"], "counters": counters}
