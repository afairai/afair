"""ADR-0008: append-only, bi-temporal entity-fact validity."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from afair.substrate import (
    REFERENCE_TIME_VERSION,
    backfill_missing_edge_validity,
    edge_is_valid_at,
    edge_visible_as_of,
    edge_was_known_at,
    effective_edge_validity,
    latest_edge_validity,
    open_db,
    sync_event_edge_validity,
    write_edge_invalidation,
    write_edge_validity_span,
    write_entity,
    write_entity_edge,
    write_event,
    write_event_temporal,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from afair.substrate.entities import EntityEdge
    from afair.substrate.events import Event


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path)
    try:
        yield conn
    finally:
        conn.close()


def _event(db: sqlite3.Connection) -> Event:
    return write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "Ada works at Acme"},
    )


def _edge(
    db: sqlite3.Connection,
    event: Event,
    *,
    valid_from: str | None = None,
    valid_to: str | None = None,
) -> EntityEdge:
    subject = write_entity(
        db,
        canonical_name="Ada",
        kind="person",
        created_by="test",
        source_event_id=event.id,
        confidence=0.95,
    )
    obj = write_entity(
        db,
        canonical_name="Acme",
        kind="organization",
        created_by="test",
        source_event_id=event.id,
        confidence=0.9,
    )
    edge = write_entity_edge(
        db,
        subject_id=subject.id,
        predicate="works_at",
        object_id=obj.id,
        source_event_id=event.id,
        discovered_by="test-canonicalizer",
        confidence=0.85,
        valid_from=valid_from,
        valid_to=valid_to,
    )
    assert edge is not None
    return edge


def test_edge_gets_reference_time_then_temporal_worker_refines_it(
    db: sqlite3.Connection,
) -> None:
    event = _event(db)
    edge = _edge(db, event)

    initial = latest_edge_validity(db, edge.id)
    assert initial is not None
    assert initial.recorded_by == REFERENCE_TIME_VERSION
    assert initial.confidence == 0.35

    temporal = write_event_temporal(
        db,
        event_id=event.id,
        event_hash=event.content_hash,
        temporal_class="one_off",
        event_time="2025-01-02",
        relevance_horizon="2025-01-03",
        confidence=0.91,
        computed_by="temporal:v2",
    )
    assert temporal is not None
    assert (
        sync_event_edge_validity(
            db,
            event_id=event.id,
            temporal_class=temporal.temporal_class,
            event_time=temporal.event_time,
            relevance_horizon=temporal.relevance_horizon,
            confidence=temporal.confidence,
            computed_by=temporal.computed_by,
        )
        == 1
    )

    refined = latest_edge_validity(db, edge.id)
    assert refined is not None
    assert refined.recorded_by == "edge-validity:temporal:v2"
    assert refined.valid_from == "2025-01-02T00:00:00+00:00"
    assert refined.valid_to == "2025-01-03T00:00:00+00:00"
    assert (
        db.execute(
            "SELECT COUNT(*) FROM edge_validity_spans WHERE edge_id = ?", (edge.id,)
        ).fetchone()[0]
        == 2
    )


def test_temporal_worker_first_is_used_when_edge_arrives_later(db: sqlite3.Connection) -> None:
    event = _event(db)
    write_event_temporal(
        db,
        event_id=event.id,
        event_hash=event.content_hash,
        temporal_class="evergreen",
        event_time="2024-06-15T10:30:00Z",
        confidence=0.8,
        computed_by="temporal:v1",
    )
    edge = _edge(db, event)

    span = latest_edge_validity(db, edge.id)
    assert span is not None
    assert span.valid_from == "2024-06-15T10:30:00+00:00"
    assert span.valid_to is None
    assert span.recorded_by == "edge-validity:temporal:v1"


def test_explicit_correction_appends_and_latest_wins(db: sqlite3.Connection) -> None:
    event = _event(db)
    edge = _edge(db, event, valid_from="2020-01-01", valid_to="2022-01-01")

    correction = write_edge_validity_span(
        db,
        edge_id=edge.id,
        valid_from="2021-02-03",
        valid_to="2023-04-05",
        recorded_by="operator:v2",
        source_event_id=event.id,
        confidence=1.0,
        reason="user corrected employment dates",
    )
    assert correction is not None
    latest = latest_edge_validity(db, edge.id)
    assert latest is not None
    assert latest.id == correction.id
    assert latest.valid_from == "2021-02-03T00:00:00+00:00"
    assert latest.valid_to == "2023-04-05T00:00:00+00:00"

    # Same worker/version and interval is a replay, not a duplicate.
    replay = write_edge_validity_span(
        db,
        edge_id=edge.id,
        valid_from="2021-02-03",
        valid_to="2023-04-05",
        recorded_by="operator:v2",
        source_event_id=event.id,
        confidence=1.0,
        reason="same replay",
    )
    assert replay is None
    assert (
        db.execute(
            "SELECT COUNT(*) FROM edge_validity_spans WHERE edge_id = ?", (edge.id,)
        ).fetchone()[0]
        == 2
    )


def test_world_time_and_knowledge_time_are_independent(db: sqlite3.Connection) -> None:
    event = _event(db)
    now = datetime.now(UTC)
    edge = _edge(
        db,
        event,
        valid_from=(now - timedelta(days=10)).isoformat(),
        valid_to=(now + timedelta(days=10)).isoformat(),
    )
    learned = datetime.now(UTC)

    assert edge_is_valid_at(db, edge.id, now)
    assert not edge_is_valid_at(db, edge.id, now + timedelta(days=11))
    assert edge_was_known_at(db, edge.id, learned)
    assert edge_visible_as_of(db, edge.id, learned)

    write_edge_invalidation(
        db,
        edge_id=edge.id,
        invalidated_by="later-event",
        reason="superseded",
        source_event_id=event.id,
    )
    after_expiry = datetime.now(UTC) + timedelta(microseconds=1)
    assert edge_is_valid_at(db, edge.id, after_expiry)
    assert not edge_was_known_at(db, edge.id, after_expiry)
    assert not edge_visible_as_of(db, edge.id, after_expiry)

    view = effective_edge_validity(db, edge.id)
    assert view is not None
    assert view.expired_at is not None


def test_validity_sidecar_is_append_only(db: sqlite3.Connection) -> None:
    event = _event(db)
    edge = _edge(db, event)
    span = latest_edge_validity(db, edge.id)
    assert span is not None
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE edge_validity_spans SET confidence = 0 WHERE id = ?", (span.id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM edge_validity_spans WHERE id = ?", (span.id,))


def test_bad_explicit_range_is_rejected_before_edge_write(db: sqlite3.Connection) -> None:
    event = _event(db)
    with pytest.raises(ValueError, match="valid_to must not precede valid_from"):
        _edge(db, event, valid_from="2025-02-01", valid_to="2025-01-01")
    assert db.execute("SELECT COUNT(*) FROM entity_edges").fetchone()[0] == 0


def test_malformed_worker_date_falls_back_without_breaking_edge_write(
    db: sqlite3.Connection,
) -> None:
    event = _event(db)
    write_event_temporal(
        db,
        event_id=event.id,
        event_hash=event.content_hash,
        temporal_class="one_off",
        event_time="not-a-date",
        confidence=0.4,
        computed_by="temporal:bad-fixture",
    )
    edge = _edge(db, event)
    span = latest_edge_validity(db, edge.id)
    assert span is not None
    assert span.recorded_by == REFERENCE_TIME_VERSION


def test_legacy_edges_backfill_in_bounded_idempotent_batches(db: sqlite3.Connection) -> None:
    event = _event(db)
    subject = write_entity(
        db,
        canonical_name="Legacy Ada",
        kind="person",
        created_by="old-canonicalizer",
        source_event_id=event.id,
        confidence=0.8,
    )
    obj = write_entity(
        db,
        canonical_name="Legacy Acme",
        kind="organization",
        created_by="old-canonicalizer",
        source_event_id=event.id,
        confidence=0.8,
    )
    db.execute(
        """
        INSERT INTO entity_edges (
            id, subject_id, predicate, object_id, valid_from, valid_to,
            discovered_at, discovered_by, source_event_id, confidence
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "legacy-edge",
            subject.id,
            "worked_at",
            obj.id,
            None,
            None,
            event.created_at,
            "old-canonicalizer",
            event.id,
            0.8,
        ),
    )
    db.commit()

    assert backfill_missing_edge_validity(db, limit=1) == {
        "examined": 1,
        "written": 1,
        "remaining": 0,
    }
    assert backfill_missing_edge_validity(db, limit=1) == {
        "examined": 0,
        "written": 0,
        "remaining": 0,
    }
    span = latest_edge_validity(db, "legacy-edge")
    assert span is not None
    assert span.recorded_by == REFERENCE_TIME_VERSION
