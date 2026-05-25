"""DDL — locked at v1, additive-only per Invariant I3.

Every statement is idempotent so init runs cleanly on either a fresh or
fully-populated database.

DO NOT EDIT or REMOVE existing statements. If new fields or new tables
are needed, APPEND new statements at the end. Old data must remain
readable, queryable, and re-interpretable by every later version.
"""

from __future__ import annotations

SCHEMA_VERSION = 1
"""Substrate writer version. Bump only when fields are ADDED, never removed."""

SCHEMA_DDL: tuple[str, ...] = (
    # ── events: the immutable append-only log ───────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS events (
        id              TEXT PRIMARY KEY,
        content_hash    TEXT NOT NULL UNIQUE,
        created_at      TEXT NOT NULL,
        origin          TEXT NOT NULL,
        kind            TEXT NOT NULL,
        payload         TEXT NOT NULL,
        parent_hashes   TEXT,
        schema_version  INTEGER NOT NULL
    ) STRICT
    """,
    # Read-path indexes — writes never UPDATE, these help SELECTs.
    "CREATE INDEX IF NOT EXISTS events_created_at_idx ON events(created_at)",
    "CREATE INDEX IF NOT EXISTS events_kind_idx       ON events(kind)",
    "CREATE INDEX IF NOT EXISTS events_origin_idx     ON events(origin)",
    # ── append-only enforcement at the DB level (Invariant I2) ──────────────
    """
    CREATE TRIGGER IF NOT EXISTS events_no_update
    BEFORE UPDATE ON events
    BEGIN
        SELECT RAISE(ABORT, 'substrate is append-only (Invariant I2)');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS events_no_delete
    BEFORE DELETE ON events
    BEGIN
        SELECT RAISE(ABORT, 'substrate is append-only (Invariant I2)');
    END
    """,
    # ── FTS5 virtual table for keyword search over derived searchable text ──
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
        content_hash UNINDEXED,
        searchable_text,
        tokenize = 'unicode61 remove_diacritics 2'
    )
    """,
    # ── interpretations: materialized views over substrate (Invariant I3) ──
    # Multiple versions may coexist. The substrate row is invariant; this
    # table is regenerable. Populated by the Extractor agent in task #4.
    """
    CREATE TABLE IF NOT EXISTS interpretations (
        id              TEXT PRIMARY KEY,
        event_id        TEXT NOT NULL REFERENCES events(id),
        event_hash      TEXT NOT NULL REFERENCES events(content_hash),
        version         INTEGER NOT NULL,
        produced_at     TEXT NOT NULL,
        produced_by     TEXT NOT NULL,
        extraction      TEXT NOT NULL,
        UNIQUE(event_hash, version, produced_by)
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS interpretations_event_idx ON interpretations(event_id)",
)
