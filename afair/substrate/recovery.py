"""Substrate recovery — rebuild SQLite from the durable event-records dir.

When the working-copy SQLite database is corrupted, deleted, or otherwise
unrecoverable, every event can be reconstructed from the immutable JSON
files under ``vault/event_records/``. That directory is the actual
source of truth; SQLite is a regenerable index over it.

This module is intentionally lean: one function rebuilds the events
table from disk, idempotently. FTS5 rows are re-derived from each
event's payload via the existing :func:`derive_searchable_text` helper.

Interpretation tables (entities, edges, mentions, consolidations,
embeddings, etc.) are NOT rebuilt here — they are materialized views
over the substrate and can be regenerated separately by their
respective worker modules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .event_records import iter_records
from .payload import canonical_json, derive_searchable_text

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


def rebuild_events_from_records(
    conn: sqlite3.Connection,
    vault_dir: Path,
) -> dict[str, int]:
    """Replay every immutable event record into the ``events`` table.

    Idempotent on content-hash: if a record already exists in SQLite,
    it is skipped (no insert, no error). Safe to run any time, including
    against a partially-populated SQLite — it only fills in missing rows.

    Returns a stats dict with ``records_seen``, ``rows_inserted``,
    ``rows_already_present``. Callers can compare against expected
    counts to verify integrity.
    """
    stats = {
        "records_seen": 0,
        "rows_inserted": 0,
        "rows_already_present": 0,
    }

    cursor = conn.cursor()
    for record in iter_records(vault_dir):
        stats["records_seen"] += 1
        chash = record["content_hash"]

        already = cursor.execute("SELECT 1 FROM events WHERE content_hash = ?", (chash,)).fetchone()
        if already is not None:
            stats["rows_already_present"] += 1
            continue

        parents_json = (
            canonical_json(record["parent_hashes"]) if record.get("parent_hashes") else None
        )

        with conn:
            conn.execute(
                """
                INSERT INTO events (
                    id, content_hash, created_at, origin, kind,
                    payload, parent_hashes, schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["id"],
                    chash,
                    record["created_at"],
                    record["origin"],
                    record["kind"],
                    canonical_json(record["payload"]),
                    parents_json,
                    record["schema_version"],
                ),
            )
            conn.execute(
                "INSERT INTO events_fts (content_hash, searchable_text) VALUES (?, ?)",
                (chash, derive_searchable_text(record["payload"])),
            )

        stats["rows_inserted"] += 1

    return stats


def backfill_records_from_events(
    conn: sqlite3.Connection,
    vault_dir: Path,
) -> dict[str, int]:
    """Backfill the event-records dir from an existing SQLite events table.

    Used once when upgrading a vault that pre-dates dual-write: every
    SQLite row gets its corresponding JSON record dropped on disk.
    Idempotent — records already present are not rewritten.
    """
    from .event_records import record_exists, write_record
    from .events import row_to_event

    stats = {"events_seen": 0, "records_written": 0, "records_already_present": 0}

    for row in conn.execute("SELECT * FROM events ORDER BY created_at ASC"):
        stats["events_seen"] += 1
        event = row_to_event(row)
        if record_exists(vault_dir, event.content_hash):
            stats["records_already_present"] += 1
            continue
        write_record(
            vault_dir,
            event_id=event.id,
            content_hash=event.content_hash,
            created_at=event.created_at,
            origin=event.origin,
            kind=event.kind,
            payload=event.payload,
            parent_hashes=event.parent_hashes,
            schema_version=event.schema_version,
        )
        stats["records_written"] += 1

    return stats
