"""Storage-layer tests for the edge-confidence overlay (ADR-0004 / S2).

Round-trip, I2 append-only enforcement, latest-wins batch resolution, and the
clean ValueError on a dangling edge_id. The pure model is tested elsewhere;
here we only exercise persistence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from afair.substrate import (
    latest_edge_confidence_batch,
    latest_edge_scores_batch,
    open_db,
    write_edge_confidence_score,
    write_entity,
    write_entity_edge,
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


def _make_edge(conn: sqlite3.Connection) -> str:
    event = write_event(
        conn, origin="user", kind="remember", payload={"content_type": "text", "text": "x runs y"}
    )
    subj = write_entity(
        conn,
        canonical_name="Alice",
        kind="person",
        created_by="test",
        source_event_id=event.id,
        confidence=0.9,
    )
    obj = write_entity(
        conn,
        canonical_name="Acme",
        kind="organization",
        created_by="test",
        source_event_id=event.id,
        confidence=0.9,
    )
    edge = write_entity_edge(
        conn,
        subject_id=subj.id,
        predicate="runs",
        object_id=obj.id,
        source_event_id=event.id,
        discovered_by="test",
        confidence=0.8,
    )
    assert edge is not None
    return edge.id


def test_write_and_read_back_roundtrip(db: sqlite3.Connection) -> None:
    edge_id = _make_edge(db)
    components = {"version": "edge_confidence:v1", "terms": {"base": 0.847}, "z": 0.847}
    score = write_edge_confidence_score(
        db,
        edge_id=edge_id,
        confidence=0.63,
        components=components,
        computed_by="edge_confidence:v1",
    )
    assert score.confidence == 0.63
    stored = latest_edge_scores_batch(db, [edge_id])[edge_id]
    assert stored.confidence == 0.63
    # Components JSON survives the round trip.
    assert stored.components == components
    assert stored.computed_by == "edge_confidence:v1"


def test_i2_no_update_no_delete(db: sqlite3.Connection) -> None:
    edge_id = _make_edge(db)
    write_edge_confidence_score(
        db, edge_id=edge_id, confidence=0.5, components={}, computed_by="edge_confidence:v1"
    )
    with pytest.raises(Exception, match="append-only"), db:
        db.execute("UPDATE edge_confidence_scores SET confidence = 0.1")
    with pytest.raises(Exception, match="append-only"), db:
        db.execute("DELETE FROM edge_confidence_scores")


def test_latest_wins_and_absent_edges(db: sqlite3.Connection) -> None:
    edge_id = _make_edge(db)
    write_edge_confidence_score(
        db, edge_id=edge_id, confidence=0.4, components={}, computed_by="edge_confidence:v1"
    )
    write_edge_confidence_score(
        db, edge_id=edge_id, confidence=0.7, components={}, computed_by="edge_confidence:v1"
    )
    served = latest_edge_confidence_batch(db, [edge_id])
    assert served[edge_id] == 0.7  # latest wins
    # Unknown edge id is absent, empty input returns {}.
    assert latest_edge_confidence_batch(db, ["edge:nope"]) == {}
    assert latest_edge_confidence_batch(db, []) == {}


def test_write_on_nonexistent_edge_raises(db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="entity_edge not found"):
        write_edge_confidence_score(
            db,
            edge_id="edge:ghost",
            confidence=0.5,
            components={},
            computed_by="edge_confidence:v1",
        )
