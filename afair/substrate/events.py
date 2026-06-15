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
    created_at: str | None = None,
    searchable_body: str | None = None,
) -> Event:
    """Insert one event, idempotent on its content hash. Returns the Event.

    Convenience wrapper around :func:`write_event_with_status` that discards
    the dedup flag — most callers only need the row itself.
    """
    event, _ = write_event_with_status(
        conn,
        origin=origin,
        kind=kind,
        payload=payload,
        parent_hashes=parent_hashes,
        created_at=created_at,
        searchable_body=searchable_body,
    )
    return event


def write_event_with_status(
    conn: sqlite3.Connection,
    *,
    origin: str,
    kind: str,
    payload: dict[str, Any],
    parent_hashes: list[str] | None = None,
    created_at: str | None = None,
    searchable_body: str | None = None,
) -> tuple[Event, bool]:
    """Insert-or-return-existing variant that also reports whether a fresh
    INSERT happened.

    ``searchable_body`` overrides the primary text that goes into the FTS5
    index — used when the body spilled to the object store (``text-large``)
    so the index still covers it even though the canonical payload only holds
    a ``blob_hash``. It does not affect the content hash (identity is
    payload-only), so two writes of the same payload still dedupe regardless.

    Returns ``(event, was_inserted)``. ``was_inserted=True`` means this call
    actually wrote the row; ``False`` means the content_hash already existed
    and the returned row was retrieved instead. Callers that need to schedule
    background work (e.g., extraction) only on a true insert use this variant
    so they don't have to do their own pre-check ``read_event_by_hash`` —
    eliminating one redundant SHA-256 + canonical_json per write (Perf audit
    minor — double content_hash).

    The payload is canonicalized (sorted keys, no whitespace) before storage
    and hashing, so insertion order of keys does not affect identity.
    """
    sorted_parents = sorted(parent_hashes) if parent_hashes else None
    chash = content_hash(kind=kind, origin=origin, payload=payload, parent_hashes=sorted_parents)

    event_id = _new_id()
    # created_at is NOT part of the content_hash (identity is kind/origin/
    # payload/parents), so an explicit timestamp is safe — used by backfills
    # and the eval harness to write events at a controlled point in time.
    created_at = created_at or _now_iso()
    payload_json = canonical_json(payload)
    parents_json = canonical_json(sorted_parents) if sorted_parents else None
    searchable = derive_searchable_text(payload, body_override=searchable_body)

    # Atomic insert-or-return-existing: the previous pattern of
    # ``read_event_by_hash → INSERT`` had a TOCTOU race — two concurrent
    # writes with the same content_hash both passed the existence check
    # on the WAL snapshot, the second INSERT then raised
    # sqlite3.IntegrityError (UNIQUE on content_hash) and propagated to
    # the MCP client as a 500. ``ON CONFLICT DO NOTHING RETURNING`` lets
    # the winner produce a row in the same statement; the loser gets
    # no row, then re-reads to find the row the winner committed
    # (audit finding — concurrency).
    with conn:
        row = conn.execute(
            """
            INSERT INTO events (
                id, content_hash, created_at, origin, kind,
                payload, parent_hashes, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(content_hash) DO NOTHING
            RETURNING id, created_at
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
        ).fetchone()
        if row is None:
            # Another writer committed the same content_hash first. Fall
            # through to the existing row — this is the idempotent path
            # the original docstring promised.
            existing = read_event_by_hash(conn, chash)
            if existing is not None:
                return existing, False
            # If we get here something is wrong with the unique index;
            # fail loud rather than returning a bogus event.
            msg = (
                f"INSERT swallowed by ON CONFLICT but content_hash {chash} "
                "not visible on follow-up read"
            )
            raise RuntimeError(msg)
        # Winner: insert the FTS row in the same transaction.
        conn.execute(
            "INSERT INTO events_fts (content_hash, searchable_text) VALUES (?, ?)",
            (chash, searchable),
        )
        # Use the actual stored values (id + created_at) in case the
        # RETURNING clause ever differs from what we sent. Defensive
        # but cheap.
        event_id = row["id"]
        created_at = row["created_at"]

    event = Event(
        id=event_id,
        content_hash=chash,
        created_at=created_at,
        origin=origin,
        kind=kind,
        payload=payload,
        parent_hashes=sorted_parents,
        schema_version=SCHEMA_VERSION,
    )
    return event, True


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
    params: list[Any] = []
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
        sql += " LIMIT ?"
        params.append(int(limit))
    for row in conn.execute(sql, params):
        yield row_to_event(row)
