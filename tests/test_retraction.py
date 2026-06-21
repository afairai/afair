"""Entity retraction — withdraw a noise entity from the live graph (I2).

A file path, a test fixture, a doc section that should never have been an
entity. Retraction is append-only (the row + mentions stay as history) and
every live-graph read filters it out: the audit stops proposing about it, the
canonicalizer/dedup stop matching to it, the article worker skips it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from afair.agents.entity_audit import EntityAuditWorker, find_cross_kind_auto_merges
from afair.settings import Settings
from afair.substrate import (
    open_db,
    retract_entity,
    retracted_entity_ids,
    write_entity,
    write_entity_merge,
    write_event,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=tmp_path,
        cold_path_enabled=False,
    )


def _entity(db: sqlite3.Connection, name: str, kind: str) -> str:
    ev = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": name}
    )
    return write_entity(
        db, canonical_name=name, kind=kind, created_by="t", source_event_id=ev.id, confidence=0.8
    ).id


# ── primitive ────────────────────────────────────────────────────────────────


def test_retract_is_append_only_and_idempotent(db: sqlite3.Connection) -> None:
    eid = _entity(db, "scripts/smoke_mcp.py", "product")
    assert retract_entity(db, entity_id=eid, retracted_by="operator", reason="noise") is True
    # The entity row still exists (I2 — history preserved).
    assert db.execute("SELECT COUNT(*) FROM entities WHERE id = ?", (eid,)).fetchone()[0] == 1
    assert retracted_entity_ids(db) == {eid}
    # Retracting again is a no-op.
    assert retract_entity(db, entity_id=eid, retracted_by="operator", reason="noise") is False


def test_retraction_row_cannot_be_deleted(db: sqlite3.Connection) -> None:
    eid = _entity(db, "smoke-test-123", "concept")
    retract_entity(db, entity_id=eid, retracted_by="operator", reason="noise")
    with pytest.raises(Exception, match="append-only"):
        db.execute("DELETE FROM entity_retractions")


# ── read-site filters ────────────────────────────────────────────────────────


def test_audit_skips_retracted_entity(db: sqlite3.Connection, settings: Settings) -> None:
    # maxime.team-as-person would normally be a retype proposal; retracted, it
    # must not be proposed at all.
    eid = _entity(db, "maxime.team", "person")
    retract_entity(db, entity_id=eid, retracted_by="operator", reason="noise")
    stats = EntityAuditWorker().run(db, settings)
    assert stats["retype_proposals"] == 0
    assert db.execute("SELECT COUNT(*) FROM proposed_corrections").fetchone()[0] == 0


def test_cross_kind_detector_skips_retracted_canonical(db: sqlite3.Connection) -> None:
    from_id = _entity(db, "scripts/smoke_mcp.py", "project")
    into_id = _entity(db, "scripts/smoke_mcp.py", "product")
    write_entity_merge(
        db,
        from_entity_id=from_id,
        into_entity_id=into_id,
        merged_by="entity_deduplicator:v0",
        reason="t",
        confidence=0.9,
    )
    assert len(find_cross_kind_auto_merges(db)) == 1  # before retraction
    retract_entity(db, entity_id=into_id, retracted_by="operator", reason="noise")
    assert find_cross_kind_auto_merges(db) == []  # canonical withdrawn → gone
