"""P1-1: the proposed_corrections open-row partial-unique migration.

A decided proposal used to block every future proposal for the same
(kind, subject) forever — for edge_review that froze ADR-0004 calibration
growth. The migration replaces the table-level ``UNIQUE(kind, entity_id)``
with a partial unique index on OPEN rows only, using the same guarded
transactional rebuild the kind-check migration already ships.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from afair.substrate import open_db, write_entity, write_event
from afair.substrate.schema import migrate_proposed_corrections_open_unique

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path

# The pre-P1 table shape: the ADR-0004 widened CHECK PLUS the inline
# UNIQUE(kind, entity_id) the migration removes.
_OLD_DDL = """
    CREATE TABLE proposed_corrections (
        id            TEXT PRIMARY KEY,
        kind          TEXT NOT NULL CHECK (kind IN ('retype', 'merge', 'merge_review', 'edge_review')),
        entity_id     TEXT NOT NULL REFERENCES entities(id),
        detail        TEXT NOT NULL,
        evidence      TEXT NOT NULL,
        confidence    REAL NOT NULL,
        tier          TEXT NOT NULL CHECK (tier IN ('auto', 'review')),
        detected_by   TEXT NOT NULL,
        detected_at   TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'proposed'
                      CHECK (status IN ('proposed', 'confirmed', 'rejected', 'applied')),
        decided_at    TEXT,
        decided_by    TEXT,
        UNIQUE(kind, entity_id)
    ) STRICT
"""


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path)
    try:
        yield conn
    finally:
        conn.close()


def _revert_to_old_shape(conn: sqlite3.Connection) -> None:
    """Rebuild proposed_corrections with the pre-P1 inline UNIQUE so we can
    exercise the forward migration on a realistic legacy table."""
    with conn:
        conn.execute("DROP TABLE proposed_corrections")
        conn.execute(_OLD_DDL)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS proposed_corrections_status_idx "
            "ON proposed_corrections(status)"
        )


def _entity(conn: sqlite3.Connection, name: str, kind: str) -> str:
    ev = write_event(
        conn, origin="user", kind="remember", payload={"content_type": "text", "text": name}
    )
    return write_entity(
        conn, canonical_name=name, kind=kind, created_by="t", source_event_id=ev.id, confidence=0.8
    ).id


def _insert(conn: sqlite3.Connection, *, pid: str, kind: str, entity_id: str, status: str) -> None:
    decided_at = None if status == "proposed" else "2026-01-01T00:00:00+00:00"
    with conn:
        conn.execute(
            """
            INSERT INTO proposed_corrections (
                id, kind, entity_id, detail, evidence, confidence, tier,
                detected_by, detected_at, status, decided_at
            ) VALUES (?, ?, ?, '{}', 'ev', 0.5, 'review', 'test', ?, ?, ?)
            """,
            (pid, kind, entity_id, "2026-01-01T00:00:00+00:00", status, decided_at),
        )


def _has_open_unique_index(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' "
        "AND name = 'proposed_corrections_open_unique'"
    ).fetchone()
    return row is not None


def test_migration_rebuilds_and_preserves_rows(db: sqlite3.Connection) -> None:
    _revert_to_old_shape(db)
    e_open = _entity(db, "OpenSubject", "product")
    e_decided = _entity(db, "DecidedSubject", "product")
    _insert(db, pid="p_open", kind="merge_review", entity_id=e_open, status="proposed")
    _insert(db, pid="p_decided", kind="edge_review", entity_id=e_decided, status="applied")

    assert migrate_proposed_corrections_open_unique(db) is True

    # Rows preserved, partial index present, inline UNIQUE gone.
    ids = {r["id"] for r in db.execute("SELECT id FROM proposed_corrections").fetchall()}
    assert ids == {"p_open", "p_decided"}
    assert _has_open_unique_index(db)
    table_sql = db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'proposed_corrections'"
    ).fetchone()["sql"]
    assert "UNIQUE(kind, entity_id)" not in table_sql


def test_migration_is_idempotent(db: sqlite3.Connection) -> None:
    _revert_to_old_shape(db)
    assert migrate_proposed_corrections_open_unique(db) is True
    # Second run: already index-based → no rebuild.
    assert migrate_proposed_corrections_open_unique(db) is False
    assert _has_open_unique_index(db)


def test_open_slot_dedupes_but_decided_frees_it(db: sqlite3.Connection) -> None:
    """After migration: a duplicate OPEN insert for the same (kind, entity) is
    rejected by the partial index; but an insert for a subject that has only a
    DECIDED row succeeds (the edge_review recycle case)."""
    _revert_to_old_shape(db)
    migrate_proposed_corrections_open_unique(db)

    subj = _entity(db, "Subj", "product")
    _insert(db, pid="e1", kind="edge_review", entity_id=subj, status="proposed")
    # A second OPEN row for the same (kind, subject) violates the partial index.
    import sqlite3 as _sqlite

    with pytest.raises(_sqlite.IntegrityError):
        _insert(db, pid="e2", kind="edge_review", entity_id=subj, status="proposed")

    # Decide the open one (status → applied); the slot is now free.
    with db:
        db.execute(
            "UPDATE proposed_corrections SET status = 'applied', "
            "decided_at = '2026-06-01T00:00:00+00:00' WHERE id = 'e1'"
        )
    # A new OPEN proposal for the same subject now inserts cleanly.
    _insert(db, pid="e3", kind="edge_review", entity_id=subj, status="proposed")
    open_rows = db.execute(
        "SELECT COUNT(*) AS n FROM proposed_corrections "
        "WHERE entity_id = ? AND status = 'proposed'",
        (subj,),
    ).fetchone()["n"]
    assert open_rows == 1
