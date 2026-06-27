"""Substrate-level tests for event_temporal (relevance-decay Phase 1).

Schema layer (I2 triggers) and the ``substrate/temporal.py`` write/read helpers.
Nothing mocked below the helpers — real SQLite, real triggers, because the
append-only guarantee is meaningless if the triggers are silent shelf-warmers.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from afair.substrate import (
    open_db,
    read_event_temporal,
    write_event,
    write_event_temporal,
)
from afair.substrate.temporal import TEMPORAL_CLASSES

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from afair.substrate.events import Event


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def sample_event(db: sqlite3.Connection) -> Event:
    return write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "dentist appointment on Friday"},
    )


def test_write_and_read_event_temporal(db: sqlite3.Connection, sample_event: Event) -> None:
    row = write_event_temporal(
        db,
        event_id=sample_event.id,
        event_hash=sample_event.content_hash,
        temporal_class="one_off",
        confidence=0.8,
        computed_by="temporal:v1",
        event_time="2026-07-03",
        relevance_horizon="2026-07-04",
    )
    assert row is not None
    assert row.temporal_class == "one_off"
    got = read_event_temporal(db, sample_event.content_hash)
    assert got is not None
    assert got.event_time == "2026-07-03"
    assert got.relevance_horizon == "2026-07-04"
    assert got.confidence == 0.8


def test_write_is_idempotent_on_event_hash_and_version(
    db: sqlite3.Connection, sample_event: Event
) -> None:
    first = write_event_temporal(
        db,
        event_id=sample_event.id,
        event_hash=sample_event.content_hash,
        temporal_class="evergreen",
        confidence=0.9,
        computed_by="temporal:v1",
    )
    assert first is not None
    second = write_event_temporal(
        db,
        event_id=sample_event.id,
        event_hash=sample_event.content_hash,
        temporal_class="evergreen",
        confidence=0.9,
        computed_by="temporal:v1",
    )
    assert second is None  # same (event_hash, computed_by) dedupes


def test_bumped_version_writes_a_new_row(db: sqlite3.Connection, sample_event: Event) -> None:
    """Re-derivation (I7): a new computed_by version is a fresh row, not a dupe."""
    v1 = write_event_temporal(
        db,
        event_id=sample_event.id,
        event_hash=sample_event.content_hash,
        temporal_class="one_off",
        confidence=0.7,
        computed_by="temporal:v1",
    )
    v2 = write_event_temporal(
        db,
        event_id=sample_event.id,
        event_hash=sample_event.content_hash,
        temporal_class="one_off",
        confidence=0.95,
        computed_by="temporal:v2",
    )
    assert v1 is not None
    assert v2 is not None


def test_update_is_blocked_by_i2_trigger(db: sqlite3.Connection, sample_event: Event) -> None:
    row = write_event_temporal(
        db,
        event_id=sample_event.id,
        event_hash=sample_event.content_hash,
        temporal_class="one_off",
        confidence=0.8,
        computed_by="temporal:v1",
    )
    assert row is not None
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE event_temporal SET confidence = 0.1 WHERE id = ?", (row.id,))


def test_delete_is_blocked_by_i2_trigger(db: sqlite3.Connection, sample_event: Event) -> None:
    row = write_event_temporal(
        db,
        event_id=sample_event.id,
        event_hash=sample_event.content_hash,
        temporal_class="one_off",
        confidence=0.8,
        computed_by="temporal:v1",
    )
    assert row is not None
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM event_temporal WHERE id = ?", (row.id,))


def test_classes_constant_covers_the_eight_spec_classes() -> None:
    assert len(TEMPORAL_CLASSES) == 8
    assert "evergreen" in TEMPORAL_CLASSES
    assert "one_off" in TEMPORAL_CLASSES
