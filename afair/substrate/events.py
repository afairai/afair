"""Event reading & writing — the substrate's primary API."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel
from ulid import ULID

from .payload import canonical_json, content_hash, derive_searchable_text
from .schema import SCHEMA_VERSION

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator


class Event(BaseModel):
    """One row of the events table — an immutable observation."""

    id: str
    content_hash: str
    created_at: str
    origin: str
    kind: str
    payload: dict[str, Any]
    parent_hashes: list[str] | None = None
    schema_version: int


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    return str(ULID())


def row_to_event(row: sqlite3.Row) -> Event:
    """Map a SQLite row to an Event. Package-internal helper."""
    return Event(
        id=row["id"],
        content_hash=row["content_hash"],
        created_at=row["created_at"],
        origin=row["origin"],
        kind=row["kind"],
        payload=json.loads(row["payload"]),
        parent_hashes=json.loads(row["parent_hashes"]) if row["parent_hashes"] else None,
        schema_version=row["schema_version"],
    )


def write_event(
    conn: sqlite3.Connection,
    *,
    origin: str,
    kind: str,
    payload: dict[str, Any],
    parent_hashes: list[str] | None = None,
) -> Event:
    """Insert one event, idempotent on its content hash.

    If an event with identical (kind, origin, payload, parent_hashes) already
    exists, returns the existing row without inserting a duplicate.

    The payload is canonicalized (sorted keys, no whitespace) before storage
    and hashing, so insertion order of keys does not affect identity.
    """
    sorted_parents = sorted(parent_hashes) if parent_hashes else None
    chash = content_hash(kind=kind, origin=origin, payload=payload, parent_hashes=sorted_parents)

    existing = read_event_by_hash(conn, chash)
    if existing is not None:
        return existing

    event_id = _new_id()
    created_at = _now_iso()
    payload_json = canonical_json(payload)
    parents_json = canonical_json(sorted_parents) if sorted_parents else None
    searchable = derive_searchable_text(payload)

    with conn:
        conn.execute(
            """
            INSERT INTO events (
                id, content_hash, created_at, origin, kind,
                payload, parent_hashes, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                chash,
                created_at,
                origin,
                kind,
                payload_json,
                parents_json,
                SCHEMA_VERSION,
            ),
        )
        conn.execute(
            "INSERT INTO events_fts (content_hash, searchable_text) VALUES (?, ?)",
            (chash, searchable),
        )

    return Event(
        id=event_id,
        content_hash=chash,
        created_at=created_at,
        origin=origin,
        kind=kind,
        payload=payload,
        parent_hashes=sorted_parents,
        schema_version=SCHEMA_VERSION,
    )


def read_event_by_id(conn: sqlite3.Connection, event_id: str) -> Event | None:
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return row_to_event(row) if row else None


def read_event_by_hash(conn: sqlite3.Connection, chash: str) -> Event | None:
    row = conn.execute("SELECT * FROM events WHERE content_hash = ?", (chash,)).fetchone()
    return row_to_event(row) if row else None


def iter_events(
    conn: sqlite3.Connection,
    *,
    kind: str | None = None,
    origin: str | None = None,
    limit: int | None = None,
    order: Literal["asc", "desc"] = "desc",
) -> Iterator[Event]:
    """Iterate events filtered by ``kind`` / ``origin``, sorted by created_at."""
    clauses: list[str] = []
    params: list[str] = []
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind)
    if origin is not None:
        clauses.append("origin = ?")
        params.append(origin)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    direction = "DESC" if order == "desc" else "ASC"
    sql = f"SELECT * FROM events {where} ORDER BY created_at {direction}"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    for row in conn.execute(sql, params):
        yield row_to_event(row)
