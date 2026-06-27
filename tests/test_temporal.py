"""Substrate-level tests for event_temporal (relevance-decay Phase 1).

Schema layer (I2 triggers) and the ``substrate/temporal.py`` write/read helpers.
Nothing mocked below the helpers — real SQLite, real triggers, because the
append-only guarantee is meaningless if the triggers are silent shelf-warmers.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from afair.substrate import (
    open_db,
    read_event_temporal,
    read_event_temporal_batch,
    temporal_relevance,
    write_event,
    write_event_temporal,
)
from afair.substrate.temporal import TEMPORAL_CLASSES, EventTemporal

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


# ── read_event_temporal_batch ───────────────────────────────────────────────


def test_batch_read_returns_latest_per_hash(db: sqlite3.Connection, sample_event: Event) -> None:
    other = write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "annual review in spring"},
    )
    write_event_temporal(
        db,
        event_id=sample_event.id,
        event_hash=sample_event.content_hash,
        temporal_class="one_off",
        confidence=0.8,
        computed_by="temporal:v1",
    )
    write_event_temporal(
        db,
        event_id=other.id,
        event_hash=other.content_hash,
        temporal_class="periodic",
        confidence=0.6,
        computed_by="temporal:v1",
    )
    got = read_event_temporal_batch(db, [sample_event.content_hash, other.content_hash])
    assert set(got) == {sample_event.content_hash, other.content_hash}
    assert got[sample_event.content_hash].temporal_class == "one_off"
    assert read_event_temporal_batch(db, []) == {}


# ── temporal_relevance (the decay multiplier) ───────────────────────────────


def _rec(temporal_class: str, **kw: object) -> EventTemporal:
    base: dict[str, object] = {
        "id": "01TEMPORAL",
        "event_id": "e1",
        "event_hash": "sha256:" + "0" * 64,
        "temporal_class": temporal_class,
        "event_time": None,
        "relevance_horizon": None,
        "recurrence_rule": None,
        "closure_state": None,
        "confidence": 1.0,
        "computed_by": "temporal:v1",
        "created_at": "2026-06-01T00:00:00+00:00",
    }
    base.update(kw)
    return EventTemporal(**base)  # type: ignore[arg-type]


_NOW = datetime(2026, 6, 27, tzinfo=UTC)


def test_evergreen_never_decays() -> None:
    assert temporal_relevance(_rec("evergreen"), _NOW) == 1.0


def test_one_off_before_horizon_is_full() -> None:
    future = (_NOW + timedelta(days=10)).isoformat()
    assert temporal_relevance(_rec("one_off", relevance_horizon=future), _NOW) == 1.0


def test_one_off_past_horizon_decays() -> None:
    past = (_NOW - timedelta(days=28)).isoformat()  # two half-lives
    factor = temporal_relevance(_rec("one_off", relevance_horizon=past), _NOW)
    assert 0.15 <= factor < 0.5


def test_superseded_is_floored_low() -> None:
    assert temporal_relevance(_rec("superseded"), _NOW) == pytest.approx(0.15)


def test_low_confidence_barely_decays() -> None:
    past = (_NOW - timedelta(days=28)).isoformat()
    strong = temporal_relevance(_rec("one_off", relevance_horizon=past, confidence=1.0), _NOW)
    weak = temporal_relevance(_rec("one_off", relevance_horizon=past, confidence=0.1), _NOW)
    assert weak > strong  # an unsure inference moves ranking far less
    assert weak > 0.85


def test_relevance_is_never_zero() -> None:
    past = (_NOW - timedelta(days=3650)).isoformat()  # a decade ago
    assert temporal_relevance(_rec("one_off", relevance_horizon=past), _NOW) >= 0.15
