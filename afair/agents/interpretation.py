"""Interpretation layer — versioned materialized views over the substrate.

Per Invariant I3, the substrate row is immutable; everything derived sits
here. Multiple interpretations may coexist per event (different versions,
different producers). The substrate may always be re-interpreted forever.

Theoretical framing: this is the **distributed semantic store** in our
Complementary Learning Systems analog (see VISION.md §6.1a). The
substrate is hippocampus-like — fast, sparse, episodic, immutable. The
interpretation layer is neocortex-like — slower, distributed, semantic,
freely revisable. The biological motivation for splitting the two
(catastrophic interference: one system can't both learn fast AND
generalize well) maps directly onto our split (I2 immutability gives us
the audit trail; I3 mutability gives us regeneration as agents improve).
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
    the existing row IF the existing row is itself a success. A prior
    ``status: failed`` row does NOT block a retry: the new row gets a fresh
    ULID + produced_at so ``read_latest_interpretation`` (which orders
    ``produced_at DESC``) prefers the success over the older failure. The
    failed row stays in the table as audit trail per I3.
    """
    existing = _read_existing(conn, event.content_hash, version=version, produced_by=produced_by)
    if existing is not None:
        if existing.extraction.get("status") != "failed":
            # Already have a successful interpretation at this (event,version,
            # producer) — idempotent no-op.
            return existing
        # Retry over a failed attempt. Disambiguate the producer string by
        # appending #retryN so the UNIQUE(event_hash, version, produced_by)
        # constraint doesn't block the new row. The failed row stays as
        # audit. ``LIKE 'extractor:%'`` still matches both; the latest-by-
        # ``produced_at`` order ensures the success row wins for recall.
        retry_n = _count_extractor_attempts(conn, event.content_hash, version, produced_by)
        produced_by = f"{produced_by}#retry{retry_n}"

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


def _count_extractor_attempts(
    conn: sqlite3.Connection, event_hash: str, version: int, base_producer: str
) -> int:
    """Count rows from this extractor (including failed/#retry variants).

    Used to derive the next ``#retryN`` suffix when the existing row is
    a failed extraction and we want to write a successful retry without
    violating the UNIQUE constraint on (event_hash, version, produced_by).
    """
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM interpretations
        WHERE event_hash = ? AND version = ?
          AND (produced_by = ? OR produced_by LIKE ? || '#retry%')
        """,
        (event_hash, version, base_producer, base_producer),
    ).fetchone()
    return int(row["n"]) if row is not None else 0


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
    """Return the most recent SUCCESSFUL Extractor interpretation for an event.

    Used by recall to surface the Extractor's distillation alongside raw
    payload text. Failed interpretations (status=failed) are skipped —
    they have no useful structured data for the AI client to act on.

    Filters by ``produced_by LIKE 'extractor:%'`` so that bind records
    written by the Bind agent (``binder:v0``) don't shadow the actual
    Extractor output. Bind records are accessed via their own helper.
    """
    import json

    row = conn.execute(
        """
        SELECT * FROM interpretations
        WHERE event_hash = ? AND produced_by LIKE 'extractor:%'
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


def read_latest_interpretations_batch(
    conn: sqlite3.Connection,
    event_hashes: list[str],
) -> dict[str, Interpretation]:
    """Batch variant — one query for N event_hashes instead of N queries.

    Used by recall to avoid the N+1 pattern of calling
    :func:`read_latest_interpretation` per hit. With limit=20 hits this
    saves ~30ms p95 on the hot path.

    Returns a dict keyed by event_hash. Hashes with no successful
    extractor interpretation are simply absent from the result (same
    semantic as the single-event variant returning None).
    """
    import json

    if not event_hashes:
        return {}

    placeholders = ",".join("?" * len(event_hashes))
    rows = conn.execute(
        f"""
        SELECT * FROM interpretations
        WHERE event_hash IN ({placeholders})
          AND produced_by LIKE 'extractor:%'
        ORDER BY event_hash, produced_at DESC, version DESC
        """,
        event_hashes,
    ).fetchall()

    # Walk rows in (hash, produced_at DESC, version DESC) order. First row
    # per hash is the latest; skip failed extractions; keep the first
    # success per hash.
    out: dict[str, Interpretation] = {}
    for row in rows:
        h = row["event_hash"]
        if h in out:
            continue  # already kept the latest for this hash
        extraction = json.loads(row["extraction"])
        if extraction.get("status") == "failed":
            # Mark with sentinel so we don't re-evaluate later rows for
            # this hash — they're all older than the failed-latest, so
            # the user-facing answer is "no usable extraction".
            out[h] = _SENTINEL_FAILED  # type: ignore[assignment]
            continue
        out[h] = Interpretation(
            id=row["id"],
            event_id=row["event_id"],
            event_hash=h,
            version=row["version"],
            produced_at=row["produced_at"],
            produced_by=row["produced_by"],
            extraction=extraction,
        )

    # Drop the failed sentinels so callers see only real Interpretations.
    return {k: v for k, v in out.items() if v is not _SENTINEL_FAILED}


# Module-private sentinel used by the batch loop to short-circuit
# multiple rows of the same hash without polluting the public type.
_SENTINEL_FAILED = object()
