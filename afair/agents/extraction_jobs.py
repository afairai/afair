"""Mutable operational queue and cross-process leases, never user memory."""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator

LEASE_SECONDS = 900
DEFER_SECONDS = 300


class ExtractionDeferred(Exception):
    """Another process owns this event, or its retry backoff has not elapsed."""


@contextmanager
def extraction_lease(
    conn: sqlite3.Connection, event_id: str, *, purpose: str = "initial"
) -> Iterator[None]:
    token = uuid4().hex
    now = time.time()
    with conn:
        conn.execute("INSERT OR IGNORE INTO extraction_jobs(event_id) VALUES (?)", (event_id,))
        claimed = conn.execute(
            """UPDATE extraction_jobs SET lease_token=?,lease_until=?,purpose=?
            WHERE event_id=? AND lease_until < ? AND available_at <= ?
            RETURNING event_id""",
            (token, now + LEASE_SECONDS, purpose, event_id, now, now),
        ).fetchone()
    if claimed is None:
        raise ExtractionDeferred("extraction is leased or deferred")
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        # Completed/failed interpretations terminate initial recovery. A budget
        # refusal or crash before an attempt lands leaves the durable job pending.
        attempted = conn.execute(
            "SELECT 1 FROM interpretations WHERE event_id=? "
            "AND produced_by LIKE 'extractor:%' LIMIT 1",
            (event_id,),
        ).fetchone()
        with conn:
            if attempted is not None and not failed:
                conn.execute(
                    "DELETE FROM extraction_jobs WHERE event_id=? AND lease_token=?",
                    (event_id, token),
                )
            else:
                conn.execute(
                    """UPDATE extraction_jobs SET lease_until=0,lease_token=NULL,
                    available_at=? WHERE event_id=? AND lease_token=?""",
                    (time.time() + DEFER_SECONDS, event_id, token),
                )
