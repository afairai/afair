"""DB surface for the event-provenance sidecar (ADR-0006).

``origin`` is part of the event content_hash (see ``events.content_hash``), so it
must stay coarse — refining it per-client would fork the dedup/hash contract.
The authenticated client is instead stamped into ``event_provenance``, an
append-only sidecar keyed by ``event_id`` and OUT of the hash. Absence of a row
means the event predates provenance or was written outside an HTTP request
(direct/in-process: unit tests, cold-path workers).

Same overlay discipline as ``edge_confidence_scores`` / ``edge_serves``: the
base row (the event) never changes; the current view is composed from the
sidecar at read time. Append-only per I2 (DB triggers enforce it). The slug is
credential-derived only — this module never sees session ids, tool-call ids, or
request headers (I4/I8 data-minimization); the caller passes an already-sanitized
slug + auth-kind in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ulid import ULID

from .sqlutil import iter_param_chunks

if TYPE_CHECKING:
    import sqlite3


@dataclass(frozen=True)
class ProvenanceRow:
    """One append-only client-provenance stamp for an event."""

    id: str
    event_id: str
    client: str
    auth_kind: str
    verb: str
    stamped_at: str


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def record_event_provenance(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    client: str,
    auth_kind: str,
    verb: str,
) -> None:
    """Stamp the writing client for an event (first time per distinct client).

    ``INSERT OR IGNORE`` on ``UNIQUE(event_id, client)``: the same client
    re-stamping the same event (e.g. an identical dedup'd remember) is a cheap
    no-op; a DIFFERENT client writing the same content-hashed event appends a
    second, honest row. Called on the write hot path — the caller wraps it
    fail-soft so a stamp failure never fails the underlying remember/observe.
    """
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO event_provenance (
                id, event_id, client, auth_kind, verb, stamped_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(ULID()), event_id, client, auth_kind, verb, _now_iso()),
        )


def read_event_provenance_batch(
    conn: sqlite3.Connection,
    event_ids: list[str],
) -> dict[str, list[ProvenanceRow]]:
    """Provenance rows per event, one query per host-parameter chunk.

    Returns a dict keyed by ``event_id``; each list is ordered by ``stamped_at``
    ascending so the FIRST element is the earliest stamp — the author. Events
    with no provenance row are simply absent (pre-provenance / non-HTTP writes).
    """
    if not event_ids:
        return {}
    out: dict[str, list[ProvenanceRow]] = {}
    for chunk in iter_param_chunks(event_ids):
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT id, event_id, client, auth_kind, verb, stamped_at "
            f"FROM event_provenance WHERE event_id IN ({placeholders}) "
            "ORDER BY stamped_at ASC, id ASC",
            chunk,
        ).fetchall()
        for row in rows:
            out.setdefault(row["event_id"], []).append(
                ProvenanceRow(
                    id=row["id"],
                    event_id=row["event_id"],
                    client=row["client"],
                    auth_kind=row["auth_kind"],
                    verb=row["verb"],
                    stamped_at=row["stamped_at"],
                )
            )
    return out


def count_events_by_client(conn: sqlite3.Connection) -> dict[str, int]:
    """Distinct events stamped per client, for the ``recall(stats=True)`` summary.

    Counts DISTINCT ``event_id`` per client so a client that re-stamped many
    events (or the honest second-client rows on dedup'd events) each count once
    per client. This is a different axis from ``by_origin`` (user/agent/worker) —
    it answers "which of my AI tools wrote to this vault".
    """
    rows = conn.execute(
        "SELECT client, COUNT(DISTINCT event_id) AS c FROM event_provenance GROUP BY client"
    ).fetchall()
    return {row["client"]: row["c"] for row in rows}
