"""Interpretation layer — versioned materialized views over the substrate.

Per Invariant I3, the substrate row is immutable; everything derived sits
here. Multiple interpretations may coexist per event (different versions,
different producers). The substrate may always be re-interpreted forever.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from ulid import ULID

from ..substrate.payload import canonical_json

if TYPE_CHECKING:
    import sqlite3

    from ..substrate.events import Event


class Interpretation(BaseModel):
    """One row of the interpretations table — regenerable derived data."""

    id: str
    event_id: str
    event_hash: str
    version: int
    produced_at: str
    produced_by: str
    extraction: dict[str, Any]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def write_interpretation(
    conn: sqlite3.Connection,
    *,
    event: Event,
    version: int,
    produced_by: str,
    extraction: dict[str, Any],
) -> Interpretation:
    """Write a successful interpretation row.

    Idempotent on (event_hash, version, produced_by) — re-running the same
    extractor on the same event at the same version is a no-op that returns
    the existing row.
    """
    existing = _read_existing(conn, event.content_hash, version=version, produced_by=produced_by)
    if existing is not None:
        return existing

    interp_id = str(ULID())
    produced_at = _now_iso()
    extraction_json = canonical_json(extraction)

    with conn:
        conn.execute(
            """
            INSERT INTO interpretations (
                id, event_id, event_hash, version, produced_at,
                produced_by, extraction
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interp_id,
                event.id,
                event.content_hash,
                version,
                produced_at,
                produced_by,
                extraction_json,
            ),
        )

    return Interpretation(
        id=interp_id,
        event_id=event.id,
        event_hash=event.content_hash,
        version=version,
        produced_at=produced_at,
        produced_by=produced_by,
        extraction=extraction,
    )


def write_failed_interpretation(
    conn: sqlite3.Connection,
    *,
    event: Event,
    version: int,
    produced_by: str,
    error_type: str,
    error_message: str,
) -> Interpretation:
    """Record a failed extraction attempt (option (b) from the design).

    Same table, same shape; the extraction JSON carries a ``status: failed``
    discriminator. Makes retry and diagnosis observable without DDL.
    """
    extraction: dict[str, Any] = {
        "status": "failed",
        "error_type": error_type,
        "error_message": error_message,
        "retries": 0,
        "attempted_at": _now_iso(),
    }
    return write_interpretation(
        conn,
        event=event,
        version=version,
        produced_by=produced_by,
        extraction=extraction,
    )


def _read_existing(
    conn: sqlite3.Connection,
    event_hash: str,
    *,
    version: int,
    produced_by: str,
) -> Interpretation | None:
    import json

    row = conn.execute(
        """
        SELECT * FROM interpretations
        WHERE event_hash = ? AND version = ? AND produced_by = ?
        """,
        (event_hash, version, produced_by),
    ).fetchone()
    if row is None:
        return None
    return Interpretation(
        id=row["id"],
        event_id=row["event_id"],
        event_hash=row["event_hash"],
        version=row["version"],
        produced_at=row["produced_at"],
        produced_by=row["produced_by"],
        extraction=json.loads(row["extraction"]),
    )


def read_latest_interpretation(conn: sqlite3.Connection, event_hash: str) -> Interpretation | None:
    """Return the most recent SUCCESSFUL interpretation for an event.

    Used by recall to surface the Extractor's distillation alongside raw
    payload text. Failed interpretations (status=failed) are skipped —
    they have no useful structured data for the AI client to act on.
    """
    import json

    row = conn.execute(
        """
        SELECT * FROM interpretations
        WHERE event_hash = ?
        ORDER BY produced_at DESC, version DESC
        """,
        (event_hash,),
    ).fetchone()
    if row is None:
        return None
    extraction = json.loads(row["extraction"])
    # Skip failed extractions for the recall path. Callers that want them
    # can query the table directly.
    if extraction.get("status") == "failed":
        return None
    return Interpretation(
        id=row["id"],
        event_id=row["event_id"],
        event_hash=row["event_hash"],
        version=row["version"],
        produced_at=row["produced_at"],
        produced_by=row["produced_by"],
        extraction=extraction,
    )
