"""Substrate recovery — defense in depth against SQLite corruption.

Every event is dual-written to (a) the SQLite events table and (b) an
immutable JSON file under ``vault/event_records/``. These tests prove
that the directory is sufficient to fully reconstruct the SQLite index
after any conceivable loss of the working DB.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from afair.substrate import (
    backfill_records_from_events,
    build_text_payload,
    iter_records,
    open_db,
    read_event_by_hash,
    rebuild_events_from_records,
    record_exists,
    record_path,
    write_event,
    write_record,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, Path]]:
    """Fresh vault — closed after test."""
    db = open_db(tmp_path)
    try:
        yield db, tmp_path
    finally:
        db.close()


# ── dual-write behavior ─────────────────────────────────────────────────────


def test_write_event_persists_record_on_disk(vault: tuple[sqlite3.Connection, Path]) -> None:
    db, vault_dir = vault
    event = write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "alpha"},
        vault_dir=vault_dir,
    )
    assert record_exists(vault_dir, event.content_hash)
    # File lives at the expected sharded path
    path = record_path(vault_dir, event.content_hash)
    assert path.exists()
    assert path.suffix == ".json"
    # Two-char shard directory
    assert len(path.parent.name) == 2


def test_dual_write_is_idempotent(vault: tuple[sqlite3.Connection, Path]) -> None:
    db, vault_dir = vault
    payload = {"content_type": "text", "text": "duplicate"}
    e1 = write_event(db, origin="user", kind="remember", payload=payload, vault_dir=vault_dir)
    e2 = write_event(db, origin="user", kind="remember", payload=payload, vault_dir=vault_dir)
    assert e1.content_hash == e2.content_hash
    # Still exactly one record file on disk
    paths = list((vault_dir / "event_records").rglob("*.json"))
    assert len(paths) == 1


def test_write_event_without_vault_dir_skips_record(
    vault: tuple[sqlite3.Connection, Path],
) -> None:
    """Tests can still write SQLite-only by passing vault_dir=None."""
    db, vault_dir = vault
    write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "no-record"},
        vault_dir=None,
    )
    # The records dir is created by open_db on every vault open, but
    # passing vault_dir=None to write_event must produce zero records.
    records_dir = vault_dir / "event_records"
    assert list(records_dir.rglob("*.json")) == []


# ── recovery ────────────────────────────────────────────────────────────────


def test_recovery_rebuilds_events_from_records(
    vault: tuple[sqlite3.Connection, Path],
    tmp_path: Path,
) -> None:
    """The full disaster-recovery scenario: SQLite gone, rebuild from disk."""
    db, vault_dir = vault

    # Write a handful of events, mixed kinds + origins.
    originals = [
        write_event(
            db,
            origin="user",
            kind="remember",
            payload=build_text_payload(
                text="first",
                context="ctx",
                type_hint="note",
                vault_dir=vault_dir,
                inline_text_max_bytes=4096,
            ),
            vault_dir=vault_dir,
        ),
        write_event(
            db,
            origin="agent:claude-code",
            kind="observe",
            payload={"content_type": "observe", "action": "edit", "subject": "file.py"},
            vault_dir=vault_dir,
        ),
        write_event(
            db,
            origin="user",
            kind="remember",
            payload={"content_type": "text", "text": "third"},
            parent_hashes=[],
            vault_dir=vault_dir,
        ),
    ]
    original_hashes = {e.content_hash for e in originals}

    # Verify the durable records are there.
    on_disk = {r["content_hash"] for r in iter_records(vault_dir)}
    assert on_disk == original_hashes

    # SIMULATE DISASTER: delete the SQLite file entirely.
    db.close()
    (vault_dir / "substrate.db").unlink()
    # Also wipe any WAL / SHM files
    for suffix in ("-wal", "-shm", "-journal"):
        wal = vault_dir / f"substrate.db{suffix}"
        if wal.exists():
            wal.unlink()

    # Rebuild from the records directory alone.
    db2 = open_db(vault_dir)
    try:
        stats = rebuild_events_from_records(db2, vault_dir)
        assert stats["records_seen"] == len(originals)
        assert stats["rows_inserted"] == len(originals)
        assert stats["rows_already_present"] == 0

        # Every original event is now back in SQLite, byte-identical payload.
        for original in originals:
            restored = read_event_by_hash(db2, original.content_hash)
            assert restored is not None
            assert restored.id == original.id
            assert restored.created_at == original.created_at
            assert restored.kind == original.kind
            assert restored.origin == original.origin
            assert restored.payload == original.payload
            assert restored.parent_hashes == original.parent_hashes
    finally:
        db2.close()


def test_recovery_is_idempotent(vault: tuple[sqlite3.Connection, Path]) -> None:
    """Running rebuild twice produces no duplicates and no errors."""
    db, vault_dir = vault
    for i in range(3):
        write_event(
            db,
            origin="user",
            kind="remember",
            payload={"content_type": "text", "text": f"event-{i}"},
            vault_dir=vault_dir,
        )

    # Second rebuild on an already-populated DB should be a no-op.
    stats = rebuild_events_from_records(db, vault_dir)
    assert stats["records_seen"] == 3
    assert stats["rows_inserted"] == 0
    assert stats["rows_already_present"] == 3


# ── backfill for pre-dual-write vaults ──────────────────────────────────────


def test_backfill_writes_records_for_legacy_events(
    vault: tuple[sqlite3.Connection, Path],
) -> None:
    """Simulate a vault that pre-dates dual-write: only SQLite, no records dir."""
    db, vault_dir = vault

    # Write events with vault_dir=None — only SQLite, no on-disk records.
    for i in range(3):
        write_event(
            db,
            origin="user",
            kind="remember",
            payload={"content_type": "text", "text": f"legacy-{i}"},
            vault_dir=None,
        )
    # Dir may exist (auto-created on open_db) but contains no record files.
    assert list((vault_dir / "event_records").rglob("*.json")) == []

    # Backfill should populate the records dir from existing SQLite rows.
    stats = backfill_records_from_events(db, vault_dir)
    assert stats["events_seen"] == 3
    assert stats["records_written"] == 3
    assert stats["records_already_present"] == 0

    on_disk = list(iter_records(vault_dir))
    assert len(on_disk) == 3

    # And a second backfill is a no-op.
    stats2 = backfill_records_from_events(db, vault_dir)
    assert stats2["records_written"] == 0
    assert stats2["records_already_present"] == 3


# ── record file format invariants ───────────────────────────────────────────


def test_record_contains_all_fields_needed_for_rebuild(
    vault: tuple[sqlite3.Connection, Path],
) -> None:
    """A single record must carry every column needed to repopulate SQLite."""
    db, vault_dir = vault
    event = write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "field-check"},
        parent_hashes=["sha256:deadbeef" + "0" * 56],
        vault_dir=vault_dir,
    )
    [record] = list(iter_records(vault_dir))
    for field in (
        "id",
        "content_hash",
        "created_at",
        "origin",
        "kind",
        "payload",
        "parent_hashes",
        "schema_version",
    ):
        assert field in record, f"record missing {field}"
    assert record["content_hash"] == event.content_hash


def test_write_record_directly_is_idempotent(tmp_path: Path) -> None:
    """write_record itself, used as a primitive, must be re-runnable."""
    write_record(
        tmp_path,
        event_id="01XYZ",
        content_hash="sha256:" + "a" * 64,
        created_at="2026-01-01T00:00:00Z",
        origin="user",
        kind="remember",
        payload={"x": 1},
        parent_hashes=None,
        schema_version=1,
    )
    # Second call: file unchanged, no exception.
    write_record(
        tmp_path,
        event_id="01XYZ",
        content_hash="sha256:" + "a" * 64,
        created_at="2026-01-01T00:00:00Z",
        origin="user",
        kind="remember",
        payload={"x": 1},
        parent_hashes=None,
        schema_version=1,
    )
    records = list(iter_records(tmp_path))
    assert len(records) == 1
