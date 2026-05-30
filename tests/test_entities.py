"""Phase 4 Track 1 — substrate-level entity-graph tests.

Covers the schema layer (DDL idempotency, I2 triggers) and the
``substrate/entities.py`` write/read helpers. The canonicalization
*logic* lives in agents/ and is tested separately.

These tests intentionally do NOT mock anything below the helpers — they
exercise real SQLite with the real triggers, because the I2 guarantee
is meaningless if the triggers are silent shelf-warmers.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from afair.substrate import (
    entity_id,
    find_edges_for_source_event,
    find_entity_by_name,
    iter_edges_for_entity,
    iter_mentions_for_event,
    open_db,
    read_edge_invalidations,
    read_entity_by_id,
    read_mentions_batch,
    resolve_canonical,
    write_edge_invalidation,
    write_entity,
    write_entity_edge,
    write_entity_mention,
    write_entity_merge,
    write_event,
)

if TYPE_CHECKING:
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
def sample_event_id(db: sqlite3.Connection) -> str:
    """One event sitting in the substrate — referenced as source by entity rows."""
    event = write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "Sajinth runs Athara"},
    )
    return event.id


# ── identity ──────────────────────────────────────────────────────────────


def test_entity_id_is_deterministic_for_same_inputs() -> None:
    a = entity_id("Sajinth", "person")
    b = entity_id("Sajinth", "person")
    assert a == b
    assert a.startswith("entity:")


def test_entity_id_is_case_insensitive_on_name() -> None:
    assert entity_id("Sajinth", "person") == entity_id("sajinth", "person")
    assert entity_id("Sajinth", "person") == entity_id("SAJINTH", "person")


def test_entity_id_differs_across_kinds() -> None:
    """'Apple' the org vs 'apple' the concept are two different IDs."""
    assert entity_id("Apple", "organization") != entity_id("apple", "concept")


def test_entity_id_strips_whitespace() -> None:
    assert entity_id("Sajinth", "person") == entity_id("  Sajinth  ", "person")


# ── DDL idempotency ───────────────────────────────────────────────────────


def test_schema_init_is_idempotent_on_populated_db(tmp_path: Path) -> None:
    """Re-opening a vault with existing entity rows must not RAISE."""
    db1 = open_db(tmp_path)
    event = write_event(
        db1, origin="user", kind="remember", payload={"content_type": "text", "text": "hi"}
    )
    write_entity(
        db1,
        canonical_name="Sajinth",
        kind="person",
        created_by="test",
        source_event_id=event.id,
        confidence=0.95,
    )
    db1.close()

    # Re-open — runs the DDL again, must not duplicate or error.
    db2 = open_db(tmp_path)
    try:
        entities = db2.execute("SELECT COUNT(*) AS n FROM entities").fetchone()
        assert entities["n"] == 1
    finally:
        db2.close()


# ── I2 triggers actually fire ─────────────────────────────────────────────


def test_entities_update_is_blocked_by_i2_trigger(
    db: sqlite3.Connection, sample_event_id: str
) -> None:
    e = write_entity(
        db,
        canonical_name="Sajinth",
        kind="person",
        created_by="test",
        source_event_id=sample_event_id,
        confidence=0.9,
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE entities SET confidence = 0.5 WHERE id = ?", (e.id,))


def test_entities_delete_is_blocked_by_i2_trigger(
    db: sqlite3.Connection, sample_event_id: str
) -> None:
    e = write_entity(
        db,
        canonical_name="Athara",
        kind="organization",
        created_by="test",
        source_event_id=sample_event_id,
        confidence=0.9,
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM entities WHERE id = ?", (e.id,))


def test_entity_mentions_update_blocked(db: sqlite3.Connection, sample_event_id: str) -> None:
    e = write_entity(
        db,
        canonical_name="X",
        kind="concept",
        created_by="test",
        source_event_id=sample_event_id,
        confidence=0.5,
    )
    event_row = db.execute(
        "SELECT id, content_hash FROM events WHERE id = ?", (sample_event_id,)
    ).fetchone()
    m = write_entity_mention(
        db,
        entity_id=e.id,
        event_id=event_row["id"],
        event_hash=event_row["content_hash"],
        surface_form="X",
        canonicalized_by="test",
        match_method="exact",
        confidence=1.0,
    )
    assert m is not None
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE entity_mentions SET confidence = 0 WHERE id = ?", (m.id,))


def test_entity_edges_delete_blocked(db: sqlite3.Connection, sample_event_id: str) -> None:
    s = write_entity(
        db,
        canonical_name="Sajinth",
        kind="person",
        created_by="test",
        source_event_id=sample_event_id,
        confidence=0.9,
    )
    o = write_entity(
        db,
        canonical_name="Athara",
        kind="organization",
        created_by="test",
        source_event_id=sample_event_id,
        confidence=0.9,
    )
    edge = write_entity_edge(
        db,
        subject_id=s.id,
        predicate="works_at",
        object_id=o.id,
        source_event_id=sample_event_id,
        discovered_by="test",
        confidence=0.85,
    )
    assert edge is not None
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM entity_edges WHERE id = ?", (edge.id,))


def test_entity_merges_self_merge_rejected(db: sqlite3.Connection, sample_event_id: str) -> None:
    """The DB-level CHECK constraint catches the trivial self-merge case,
    AND the Python helper rejects it before SQL hits."""
    e = write_entity(
        db,
        canonical_name="X",
        kind="concept",
        created_by="test",
        source_event_id=sample_event_id,
        confidence=0.5,
    )
    # Python guard.
    from afair.substrate import write_entity_merge as wem

    with pytest.raises(ValueError, match="cannot merge an entity into itself"):
        wem(
            db,
            from_entity_id=e.id,
            into_entity_id=e.id,
            merged_by="test",
            reason="circular",
            confidence=1.0,
        )


# ── write helpers ─────────────────────────────────────────────────────────


def test_write_entity_returns_existing_on_repeat(
    db: sqlite3.Connection, sample_event_id: str
) -> None:
    """Per-I2: a second call with same (canonical_name, kind) returns the
    original row unchanged. Confidence on the first call wins."""
    first = write_entity(
        db,
        canonical_name="Sajinth",
        kind="person",
        created_by="canonicalizer:v0",
        source_event_id=sample_event_id,
        confidence=0.8,
    )
    second = write_entity(
        db,
        canonical_name="Sajinth",
        kind="person",
        created_by="canonicalizer:v0",
        source_event_id=sample_event_id,
        confidence=0.5,
    )
    assert first.id == second.id
    assert second.confidence == 0.8  # original wins


def test_find_entity_by_name_case_insensitive(db: sqlite3.Connection, sample_event_id: str) -> None:
    write_entity(
        db,
        canonical_name="Sajinth",
        kind="person",
        created_by="test",
        source_event_id=sample_event_id,
        confidence=1.0,
    )
    assert len(find_entity_by_name(db, canonical_name="sajinth", kind="person")) == 1
    assert len(find_entity_by_name(db, canonical_name="SAJINTH", kind="person")) == 1


def test_find_entity_by_name_disambiguates_by_kind(
    db: sqlite3.Connection, sample_event_id: str
) -> None:
    write_entity(
        db,
        canonical_name="Apple",
        kind="organization",
        created_by="test",
        source_event_id=sample_event_id,
        confidence=1.0,
    )
    write_entity(
        db,
        canonical_name="apple",
        kind="concept",
        created_by="test",
        source_event_id=sample_event_id,
        confidence=1.0,
    )
    # Without kind filter — both come back.
    assert len(find_entity_by_name(db, canonical_name="apple")) == 2
    # With kind — only the matching one.
    org_hits = find_entity_by_name(db, canonical_name="apple", kind="organization")
    assert len(org_hits) == 1
    assert org_hits[0].kind == "organization"


def test_write_entity_mention_is_idempotent_on_unique_key(
    db: sqlite3.Connection, sample_event_id: str
) -> None:
    e = write_entity(
        db,
        canonical_name="Sajinth",
        kind="person",
        created_by="test",
        source_event_id=sample_event_id,
        confidence=0.9,
    )
    event_row = db.execute(
        "SELECT id, content_hash FROM events WHERE id = ?", (sample_event_id,)
    ).fetchone()
    m1 = write_entity_mention(
        db,
        entity_id=e.id,
        event_id=event_row["id"],
        event_hash=event_row["content_hash"],
        surface_form="Sajinth",
        canonicalized_by="test",
        match_method="exact",
        confidence=1.0,
    )
    assert m1 is not None
    m2 = write_entity_mention(
        db,
        entity_id=e.id,
        event_id=event_row["id"],
        event_hash=event_row["content_hash"],
        surface_form="Sajinth",
        canonicalized_by="test",
        match_method="exact",
        confidence=1.0,
    )
    # Duplicate suppressed — UNIQUE constraint absorbed.
    assert m2 is None
    # Single row landed.
    count = db.execute("SELECT COUNT(*) AS n FROM entity_mentions").fetchone()["n"]
    assert count == 1


def test_write_entity_mention_rejects_bad_match_method(
    db: sqlite3.Connection, sample_event_id: str
) -> None:
    e = write_entity(
        db,
        canonical_name="X",
        kind="concept",
        created_by="test",
        source_event_id=sample_event_id,
        confidence=1.0,
    )
    event_row = db.execute(
        "SELECT id, content_hash FROM events WHERE id = ?", (sample_event_id,)
    ).fetchone()
    with pytest.raises(ValueError, match="match_method"):
        write_entity_mention(
            db,
            entity_id=e.id,
            event_id=event_row["id"],
            event_hash=event_row["content_hash"],
            surface_form="X",
            canonicalized_by="test",
            match_method="invented",
            confidence=1.0,
        )


def test_write_entity_edge_idempotent_on_same_source(
    db: sqlite3.Connection, sample_event_id: str
) -> None:
    s = write_entity(
        db,
        canonical_name="Sajinth",
        kind="person",
        created_by="test",
        source_event_id=sample_event_id,
        confidence=0.9,
    )
    o = write_entity(
        db,
        canonical_name="Athara",
        kind="organization",
        created_by="test",
        source_event_id=sample_event_id,
        confidence=0.9,
    )
    first = write_entity_edge(
        db,
        subject_id=s.id,
        predicate="works_at",
        object_id=o.id,
        source_event_id=sample_event_id,
        discovered_by="test",
        confidence=0.85,
    )
    second = write_entity_edge(
        db,
        subject_id=s.id,
        predicate="works_at",
        object_id=o.id,
        source_event_id=sample_event_id,
        discovered_by="test",
        confidence=0.85,
    )
    assert first is not None
    assert second is None  # UNIQUE absorbed
    count = db.execute("SELECT COUNT(*) AS n FROM entity_edges").fetchone()["n"]
    assert count == 1


def test_write_entity_edge_different_sources_coexist(
    db: sqlite3.Connection, sample_event_id: str
) -> None:
    """Same (subject, predicate, object) but discovered from two events —
    two rows. Each event independently saw the relation; both kept."""
    other_event = write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "Sajinth still leads Athara"},
    )
    s = write_entity(
        db,
        canonical_name="S",
        kind="person",
        created_by="test",
        source_event_id=sample_event_id,
        confidence=0.9,
    )
    o = write_entity(
        db,
        canonical_name="A",
        kind="organization",
        created_by="test",
        source_event_id=sample_event_id,
        confidence=0.9,
    )
    write_entity_edge(
        db,
        subject_id=s.id,
        predicate="works_at",
        object_id=o.id,
        source_event_id=sample_event_id,
        discovered_by="test",
        confidence=0.8,
    )
    write_entity_edge(
        db,
        subject_id=s.id,
        predicate="works_at",
        object_id=o.id,
        source_event_id=other_event.id,
        discovered_by="test",
        confidence=0.9,
    )
    edges = iter_edges_for_entity(db, s.id)
    assert len(edges) == 2


# ── merge transitive closure ──────────────────────────────────────────────


def test_resolve_canonical_follows_single_merge(
    db: sqlite3.Connection, sample_event_id: str
) -> None:
    a = write_entity(
        db,
        canonical_name="Sajinth-elvah",
        kind="person",
        created_by="test",
        source_event_id=sample_event_id,
        confidence=0.7,
    )
    b = write_entity(
        db,
        canonical_name="Sajinth",
        kind="person",
        created_by="test",
        source_event_id=sample_event_id,
        confidence=0.9,
    )
    write_entity_merge(
        db,
        from_entity_id=a.id,
        into_entity_id=b.id,
        merged_by="canonicalizer:v0",
        reason="LLM identified same individual",
        confidence=0.92,
    )
    assert resolve_canonical(db, a.id) == b.id
    assert resolve_canonical(db, b.id) == b.id  # unmerged entity is its own canonical


def test_resolve_canonical_follows_chain(db: sqlite3.Connection, sample_event_id: str) -> None:
    """A→B→C should resolve A to C."""
    a = write_entity(
        db,
        canonical_name="A",
        kind="concept",
        created_by="t",
        source_event_id=sample_event_id,
        confidence=0.5,
    )
    b = write_entity(
        db,
        canonical_name="B",
        kind="concept",
        created_by="t",
        source_event_id=sample_event_id,
        confidence=0.5,
    )
    c = write_entity(
        db,
        canonical_name="C",
        kind="concept",
        created_by="t",
        source_event_id=sample_event_id,
        confidence=0.5,
    )
    write_entity_merge(
        db,
        from_entity_id=a.id,
        into_entity_id=b.id,
        merged_by="t",
        reason="x",
        confidence=1.0,
    )
    write_entity_merge(
        db,
        from_entity_id=b.id,
        into_entity_id=c.id,
        merged_by="t",
        reason="y",
        confidence=1.0,
    )
    assert resolve_canonical(db, a.id) == c.id


def test_resolve_canonical_batch_matches_per_id_helper(
    db: sqlite3.Connection, sample_event_id: str
) -> None:
    """Batched CTE walk must produce the same mapping as the per-id loop."""
    from afair.substrate.entities import resolve_canonical_batch

    a = write_entity(
        db,
        canonical_name="A",
        kind="concept",
        created_by="t",
        source_event_id=sample_event_id,
        confidence=0.5,
    )
    b = write_entity(
        db,
        canonical_name="B",
        kind="concept",
        created_by="t",
        source_event_id=sample_event_id,
        confidence=0.5,
    )
    c = write_entity(
        db,
        canonical_name="C",
        kind="concept",
        created_by="t",
        source_event_id=sample_event_id,
        confidence=0.5,
    )
    d = write_entity(
        db,
        canonical_name="D",
        kind="concept",
        created_by="t",
        source_event_id=sample_event_id,
        confidence=0.5,
    )
    # A → B → C (chain of length 2); D stays canonical.
    write_entity_merge(
        db, from_entity_id=a.id, into_entity_id=b.id, merged_by="t", reason="x", confidence=1.0
    )
    write_entity_merge(
        db, from_entity_id=b.id, into_entity_id=c.id, merged_by="t", reason="y", confidence=1.0
    )

    inputs = [a.id, b.id, c.id, d.id]
    expected = {eid: resolve_canonical(db, eid) for eid in inputs}
    actual = resolve_canonical_batch(db, inputs)

    assert actual == expected
    assert actual[a.id] == c.id
    assert actual[d.id] == d.id


def test_resolve_canonical_batch_picks_latest_merge_row(
    db: sqlite3.Connection, sample_event_id: str
) -> None:
    """When the same from_entity_id has two merge rows, latest merged_at wins.

    The per-id helper uses ORDER BY merged_at DESC LIMIT 1. The batched
    CTE must do the same via the latest_merges CTE rank.
    """
    from afair.substrate.entities import resolve_canonical_batch

    a = write_entity(
        db,
        canonical_name="A",
        kind="concept",
        created_by="t",
        source_event_id=sample_event_id,
        confidence=0.5,
    )
    b = write_entity(
        db,
        canonical_name="B",
        kind="concept",
        created_by="t",
        source_event_id=sample_event_id,
        confidence=0.5,
    )
    c = write_entity(
        db,
        canonical_name="C",
        kind="concept",
        created_by="t",
        source_event_id=sample_event_id,
        confidence=0.5,
    )
    # Two sequential merges from the same source. _now_iso() uses
    # datetime.now() with microsecond precision so the second row has
    # a strictly later merged_at; ROW_NUMBER ORDER BY merged_at DESC
    # picks A → C as the winner.
    import time

    write_entity_merge(
        db, from_entity_id=a.id, into_entity_id=b.id, merged_by="t", reason="first", confidence=0.6
    )
    time.sleep(0.001)  # guarantee distinct microsecond stamps even on fast hosts
    write_entity_merge(
        db, from_entity_id=a.id, into_entity_id=c.id, merged_by="t", reason="second", confidence=0.9
    )

    assert resolve_canonical(db, a.id) == c.id
    assert resolve_canonical_batch(db, [a.id]) == {a.id: c.id}


def test_resolve_canonical_batch_empty_and_dedup(
    db: sqlite3.Connection, sample_event_id: str
) -> None:
    """Empty input → empty dict; duplicates collapse to one entry."""
    from afair.substrate.entities import resolve_canonical_batch

    a = write_entity(
        db,
        canonical_name="A",
        kind="concept",
        created_by="t",
        source_event_id=sample_event_id,
        confidence=0.5,
    )

    assert resolve_canonical_batch(db, []) == {}
    assert resolve_canonical_batch(db, [a.id, a.id, a.id]) == {a.id: a.id}


# ── edge invalidation ─────────────────────────────────────────────────────


def test_iter_edges_hides_invalidated_by_default(
    db: sqlite3.Connection, sample_event_id: str
) -> None:
    """Decision #6: superseded edges hidden unless include_invalidated=True."""
    s = write_entity(
        db,
        canonical_name="S",
        kind="person",
        created_by="t",
        source_event_id=sample_event_id,
        confidence=0.9,
    )
    o = write_entity(
        db,
        canonical_name="A",
        kind="organization",
        created_by="t",
        source_event_id=sample_event_id,
        confidence=0.9,
    )
    edge = write_entity_edge(
        db,
        subject_id=s.id,
        predicate="works_at",
        object_id=o.id,
        source_event_id=sample_event_id,
        discovered_by="t",
        confidence=0.85,
    )
    assert edge is not None
    write_edge_invalidation(
        db,
        edge_id=edge.id,
        invalidated_by="event:later",
        reason="user updated the fact",
    )
    visible = iter_edges_for_entity(db, s.id)
    assert visible == []
    historical = iter_edges_for_entity(db, s.id, include_invalidated=True)
    assert len(historical) == 1


def test_read_edge_invalidations_in_chronological_order(
    db: sqlite3.Connection, sample_event_id: str
) -> None:
    s = write_entity(
        db,
        canonical_name="S",
        kind="person",
        created_by="t",
        source_event_id=sample_event_id,
        confidence=0.9,
    )
    o = write_entity(
        db,
        canonical_name="O",
        kind="organization",
        created_by="t",
        source_event_id=sample_event_id,
        confidence=0.9,
    )
    edge = write_entity_edge(
        db,
        subject_id=s.id,
        predicate="works_at",
        object_id=o.id,
        source_event_id=sample_event_id,
        discovered_by="t",
        confidence=0.85,
    )
    assert edge is not None
    a = write_edge_invalidation(db, edge_id=edge.id, invalidated_by="x", reason="first")
    b = write_edge_invalidation(db, edge_id=edge.id, invalidated_by="y", reason="second")
    assert a is not None
    assert b is not None
    invalidations = read_edge_invalidations(db, edge.id)
    assert len(invalidations) == 2
    assert invalidations[0].reason == "first"
    assert invalidations[1].reason == "second"


def test_edge_invalidation_idempotent_on_same_source(
    db: sqlite3.Connection, sample_event_id: str
) -> None:
    """Re-cascading the same invalidate event over the same edge is a no-op."""
    s = write_entity(
        db,
        canonical_name="S",
        kind="person",
        created_by="t",
        source_event_id=sample_event_id,
        confidence=0.9,
    )
    o = write_entity(
        db,
        canonical_name="O",
        kind="organization",
        created_by="t",
        source_event_id=sample_event_id,
        confidence=0.9,
    )
    edge = write_entity_edge(
        db,
        subject_id=s.id,
        predicate="works_at",
        object_id=o.id,
        source_event_id=sample_event_id,
        discovered_by="t",
        confidence=0.85,
    )
    assert edge is not None
    first = write_edge_invalidation(
        db,
        edge_id=edge.id,
        invalidated_by="event:abc",
        reason="cascade",
        source_event_id=sample_event_id,
    )
    second = write_edge_invalidation(
        db,
        edge_id=edge.id,
        invalidated_by="event:abc",
        reason="cascade",
        source_event_id=sample_event_id,
    )
    assert first is not None
    assert second is None


# ── batch read used by recall ─────────────────────────────────────────────


def test_read_mentions_batch_groups_by_event_hash(
    db: sqlite3.Connection, sample_event_id: str
) -> None:
    """Recall calls this once per recall to attach mentions to all hits."""
    other_event = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": "other"}
    )
    s = write_entity(
        db,
        canonical_name="S",
        kind="person",
        created_by="t",
        source_event_id=sample_event_id,
        confidence=0.9,
    )
    a = write_entity(
        db,
        canonical_name="A",
        kind="organization",
        created_by="t",
        source_event_id=sample_event_id,
        confidence=0.9,
    )
    e1 = db.execute(
        "SELECT id, content_hash FROM events WHERE id = ?", (sample_event_id,)
    ).fetchone()
    e2 = db.execute(
        "SELECT id, content_hash FROM events WHERE id = ?", (other_event.id,)
    ).fetchone()
    write_entity_mention(
        db,
        entity_id=s.id,
        event_id=e1["id"],
        event_hash=e1["content_hash"],
        surface_form="Sajinth",
        canonicalized_by="t",
        match_method="exact",
        confidence=1.0,
    )
    write_entity_mention(
        db,
        entity_id=a.id,
        event_id=e1["id"],
        event_hash=e1["content_hash"],
        surface_form="Athara",
        canonicalized_by="t",
        match_method="exact",
        confidence=1.0,
    )
    write_entity_mention(
        db,
        entity_id=s.id,
        event_id=e2["id"],
        event_hash=e2["content_hash"],
        surface_form="Saji",
        canonicalized_by="t",
        match_method="llm",
        confidence=0.7,
    )
    batch = read_mentions_batch(db, [e1["content_hash"], e2["content_hash"]])
    assert set(batch.keys()) == {e1["content_hash"], e2["content_hash"]}
    assert len(batch[e1["content_hash"]]) == 2
    assert len(batch[e2["content_hash"]]) == 1


def test_read_mentions_batch_empty_input_returns_empty(db: sqlite3.Connection) -> None:
    assert read_mentions_batch(db, []) == {}


def test_iter_mentions_for_event(db: sqlite3.Connection, sample_event_id: str) -> None:
    s = write_entity(
        db,
        canonical_name="S",
        kind="person",
        created_by="t",
        source_event_id=sample_event_id,
        confidence=0.9,
    )
    e1 = db.execute(
        "SELECT id, content_hash FROM events WHERE id = ?", (sample_event_id,)
    ).fetchone()
    write_entity_mention(
        db,
        entity_id=s.id,
        event_id=e1["id"],
        event_hash=e1["content_hash"],
        surface_form="Sajinth",
        canonicalized_by="t",
        match_method="exact",
        confidence=1.0,
    )
    mentions = iter_mentions_for_event(db, e1["content_hash"])
    assert len(mentions) == 1
    assert mentions[0].surface_form == "Sajinth"


def test_find_edges_for_source_event(db: sqlite3.Connection, sample_event_id: str) -> None:
    """Used by the cascade-invalidation step in Stage 2."""
    s = write_entity(
        db,
        canonical_name="S",
        kind="person",
        created_by="t",
        source_event_id=sample_event_id,
        confidence=0.9,
    )
    o = write_entity(
        db,
        canonical_name="O",
        kind="organization",
        created_by="t",
        source_event_id=sample_event_id,
        confidence=0.9,
    )
    write_entity_edge(
        db,
        subject_id=s.id,
        predicate="works_at",
        object_id=o.id,
        source_event_id=sample_event_id,
        discovered_by="t",
        confidence=0.85,
    )
    edges = find_edges_for_source_event(db, sample_event_id)
    assert len(edges) == 1
    assert edges[0].predicate == "works_at"


def test_read_entity_by_id_returns_none_when_missing(db: sqlite3.Connection) -> None:
    assert read_entity_by_id(db, "entity:does-not-exist") is None
