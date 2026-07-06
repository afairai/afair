"""worker_watermarks — cursor read/write + monotonic-advance guard (P2a).

The watermark table is MUTABLE derived state (I2 exception, like
proposed_corrections): no append-only triggers, deleting a row is a lossless
re-scan. The cursor is a ULID id, so a monotonic forward-only guard keeps it
retry- and crash-safe.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from afair.substrate import open_db, watermarks

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


def test_read_watermark_none_on_fresh_db(db: sqlite3.Connection) -> None:
    assert watermarks.read_watermark(db, watermarks.WORKER_SALIENCE) is None
    assert watermarks.read_watermark_id(db, watermarks.WORKER_SALIENCE) is None


def test_write_then_read_roundtrips(db: sqlite3.Connection) -> None:
    watermarks.write_watermark(
        db,
        watermarks.WORKER_TEMPORAL,
        through_created_at="2026-07-06T00:00:00+00:00",
        through_id="01A",
    )
    assert watermarks.read_watermark(db, watermarks.WORKER_TEMPORAL) == (
        "2026-07-06T00:00:00+00:00",
        "01A",
    )
    assert watermarks.read_watermark_id(db, watermarks.WORKER_TEMPORAL) == "01A"


def test_write_advances_forward(db: sqlite3.Connection) -> None:
    watermarks.write_watermark(
        db, watermarks.WORKER_SALIENCE, through_created_at="t1", through_id="01A"
    )
    watermarks.write_watermark(
        db, watermarks.WORKER_SALIENCE, through_created_at="t2", through_id="01B"
    )
    assert watermarks.read_watermark_id(db, watermarks.WORKER_SALIENCE) == "01B"


def test_monotonic_guard_rejects_backwards_advance(db: sqlite3.Connection) -> None:
    """A lower (or equal) id must NOT overwrite the cursor — this is the
    crash/retry-safety guarantee that lets a re-run never move backwards."""
    watermarks.write_watermark(
        db, watermarks.WORKER_SALIENCE, through_created_at="t2", through_id="01B"
    )
    # Backwards id → no-op.
    watermarks.write_watermark(
        db, watermarks.WORKER_SALIENCE, through_created_at="t1", through_id="01A"
    )
    assert watermarks.read_watermark_id(db, watermarks.WORKER_SALIENCE) == "01B"
    # Equal id → also no-op.
    watermarks.write_watermark(
        db, watermarks.WORKER_SALIENCE, through_created_at="t3", through_id="01B"
    )
    assert watermarks.read_watermark(db, watermarks.WORKER_SALIENCE) == ("t2", "01B")


def test_watermarks_are_mutable_no_append_only_triggers(db: sqlite3.Connection) -> None:
    """Unlike substrate tables, worker_watermarks has NO append-only triggers:
    a forward UPDATE (the upsert path) and a DELETE (lossless re-scan) both
    succeed."""
    watermarks.write_watermark(
        db, watermarks.WORKER_SALIENCE, through_created_at="t1", through_id="01A"
    )
    # UPDATE succeeds (the ON CONFLICT DO UPDATE forward path already proves it).
    db.execute("DELETE FROM worker_watermarks WHERE worker = ?", (watermarks.WORKER_SALIENCE,))
    db.commit()
    assert watermarks.read_watermark(db, watermarks.WORKER_SALIENCE) is None


def test_frontier_none_on_empty_vault(db: sqlite3.Connection) -> None:
    assert watermarks.frontier_events(db) is None
    assert watermarks.frontier_interpretations(db) is None


# ── worker integration: salience (no LLM, pure) ──────────────────────────────


@pytest.fixture
def settings(tmp_path: Path):  # type: ignore[no-untyped-def]
    from afair.settings import Settings

    return Settings(_env_file=None, environment="local", vault_dir=tmp_path)  # type: ignore[call-arg]


def _seed_remember(db: sqlite3.Connection, text: str) -> str:
    from afair.substrate import write_event

    ev = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": text}
    )
    return ev.id


def test_salience_watermark_advances_on_drain_and_skips(db, settings) -> None:  # type: ignore[no-untyped-def]
    from afair.agents.salience import SalienceWorker

    e1 = _seed_remember(db, "first")
    e2 = _seed_remember(db, "second")
    frontier = watermarks.frontier_events(db)
    assert frontier is not None and frontier[1] == max(e1, e2)  # ULID monotonic

    SalienceWorker().run(db, settings)
    # Fully drained (2 < batch limit, both scored) → watermark advances to the
    # newest event id.
    assert watermarks.read_watermark_id(db, watermarks.WORKER_SALIENCE) == frontier[1]

    # A re-run with no new events finds zero candidates.
    stats = SalienceWorker().run(db, settings)
    assert stats["candidates"] == 0

    # One new event: only IT is a candidate (everything below the cursor is
    # skipped), and the watermark advances again.
    e3 = _seed_remember(db, "third")
    stats = SalienceWorker().run(db, settings)
    assert stats["candidates"] == 1
    assert stats["scored"] == 1
    assert watermarks.read_watermark_id(db, watermarks.WORKER_SALIENCE) == e3


def test_salience_watermark_not_advanced_when_batch_capped(db, settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A limit-capped cycle (backlog remains) must NOT advance the watermark —
    otherwise the un-selected backlog below the frontier would be skipped."""
    from afair.agents import salience

    monkeypatch.setattr(salience, "SALIENCE_BATCH_LIMIT", 2)
    for i in range(3):
        _seed_remember(db, f"e{i}")

    stats = salience.SalienceWorker().run(db, settings)
    assert stats["candidates"] == 2  # capped
    # Backlog remains → cursor stays unset so the 3rd event is never skipped.
    assert watermarks.read_watermark_id(db, watermarks.WORKER_SALIENCE) is None

    # Next cycle drains the remaining one → now it may advance.
    stats = salience.SalienceWorker().run(db, settings)
    assert stats["candidates"] == 1
    assert watermarks.read_watermark_id(db, watermarks.WORKER_SALIENCE) is not None


# ── skip-safety: interpretation-id cursor beats a past-dated event ───────────


def test_interp_cursor_selects_late_interp_for_old_event(db) -> None:  # type: ignore[no-untyped-def]
    """A late-arriving extractor interpretation for an OLD (past-dated) event
    must still be selected: the cursor keys on the interpretation id (minted
    now, above the watermark), NOT the event's created_at. A created_at cursor
    would skip it."""
    from afair.agents.entity_canonicalizer import _find_uncanonicalized_events
    from afair.agents.extraction_retry import select_retry_candidates
    from afair.agents.interpretation import write_interpretation
    from afair.agents.temporal import _find_events_needing_temporal
    from afair.substrate import write_event

    # An event dated far in the past, whose extractor interpretation is written
    # NOW (fresh ULID id) — simulating a delayed/retried extraction.
    old_event = write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "old"},
        created_at="2020-01-01T00:00:00+00:00",
    )
    interp = write_interpretation(
        db,
        event=old_event,
        version=1,
        produced_by="extractor:anthropic/claude-haiku-4-5",
        extraction={"status": "success", "entities": [], "relations": []},
    )

    # A watermark set BELOW the fresh interpretation id (simulating a prior
    # cursor from before this interp existed).
    low_cursor = "00000000000000000000000000"
    assert interp.id > low_cursor

    # All three interpretation-based selectors include the late interp because
    # its id > the watermark, despite the event's ancient created_at.
    temporal = _find_events_needing_temporal(db, 10, wm_id=low_cursor)
    assert any(e.id == old_event.id for e, _ in temporal)

    canon = _find_uncanonicalized_events(db, 10, wm_id=low_cursor)
    assert any(e.id == old_event.id for e, _ in canon)

    # A watermark AT/above the interp id correctly excludes it (already drained).
    assert _find_uncanonicalized_events(db, 10, wm_id=interp.id) == []
    assert _find_events_needing_temporal(db, 10, wm_id=interp.id) == []
    # (extraction_retry only selects FAILED-latest events; this success is not a
    # candidate there — asserted by the empty result below.)
    assert select_retry_candidates(db, wm_id=low_cursor) == []
