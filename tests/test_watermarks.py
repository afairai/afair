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


def _disable_frontier_lag(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Push the lag horizon into the future so freshly-seeded events count
    toward the frontier — isolates the drain-advance MECHANICS from the
    concurrency lag (which has its own dedicated regression test below)."""
    monkeypatch.setattr(watermarks, "FRONTIER_LAG_SECONDS", -3600)


def test_salience_watermark_advances_on_drain_and_skips(db, settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from afair.agents.salience import SalienceWorker

    _disable_frontier_lag(monkeypatch)
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

    _disable_frontier_lag(monkeypatch)
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


# ── R1: the frontier lags so concurrent pre-minted ids can't be stranded ─────


def _insert_event_with_id(db: sqlite3.Connection, event_id: str, *, created_at: str) -> None:
    """Insert an event row with a caller-chosen id (to place it on either side
    of the lag horizon). Bypasses write_event, which mints its own id."""
    with db:
        db.execute(
            "INSERT INTO events (id, content_hash, created_at, origin, kind, payload, "
            "parent_hashes, schema_version) VALUES (?, ?, ?, 'user', 'remember', '{}', NULL, 1)",
            (event_id, "sha256:" + event_id, created_at),
        )


def test_frontier_lags_recent_window(db: sqlite3.Connection) -> None:
    """R1 regression: the advanceable frontier EXCLUDES ids minted within the
    last FRONTIER_LAG_SECONDS. A row committed just now (fresh id) must NOT
    become the frontier — otherwise a concurrent writer that pre-minted a
    SMALLER id and commits a moment later is stranded below the cursor forever.

    RED against the pre-fix un-lagged ``MAX(id)`` frontier: it returned the
    fresh id; here the fresh id is invisible and only the > lag-old id shows."""
    from datetime import UTC, datetime, timedelta

    from ulid import ULID

    # A "fresh" event minted now (inside the lag window).
    fresh_id = str(ULID())
    _insert_event_with_id(db, fresh_id, created_at=datetime.now(UTC).isoformat())
    # The frontier must skip it — nothing is old enough yet.
    assert watermarks.frontier_events(db) is None

    # An event whose id timestamp is safely OLDER than the lag horizon.
    old_id = str(
        ULID.from_datetime(
            datetime.now(UTC) - timedelta(seconds=watermarks.FRONTIER_LAG_SECONDS + 30)
        )
    )
    _insert_event_with_id(db, old_id, created_at=datetime.now(UTC).isoformat())

    frontier = watermarks.frontier_events(db)
    # The frontier is the OLD id, never the fresher one — proving a
    # concurrently-committing pre-minted id (which would sort above old_id and
    # below fresh_id) can still be selected next cycle.
    assert frontier is not None
    assert frontier[1] == old_id
    assert frontier[1] < fresh_id


def test_frontier_interpretations_lags_recent_window(db: sqlite3.Connection) -> None:
    """Same lag guarantee for the interpretation frontier."""
    from datetime import UTC, datetime, timedelta

    from ulid import ULID

    from afair.agents.interpretation import write_interpretation
    from afair.substrate import write_event

    ev = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": "x"}
    )
    write_interpretation(
        db,
        event=ev,
        version=1,
        produced_by="extractor:test",
        extraction={"status": "success", "entities": [], "relations": []},
    )
    # Fresh interpretation → excluded by the lag.
    assert watermarks.frontier_interpretations(db) is None
    # A second event with an interpretation carrying an id OLDER than the lag
    # horizon (crafted via a raw insert to control the id).
    ev2 = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": "y"}
    )
    old_id = str(
        ULID.from_datetime(
            datetime.now(UTC) - timedelta(seconds=watermarks.FRONTIER_LAG_SECONDS + 30)
        )
    )
    with db:
        db.execute(
            "INSERT INTO interpretations (id, event_id, event_hash, version, produced_at, "
            "produced_by, extraction) VALUES (?, ?, ?, 1, ?, 'extractor:test', '{}')",
            (old_id, ev2.id, ev2.content_hash, datetime.now(UTC).isoformat()),
        )
    frontier = watermarks.frontier_interpretations(db)
    assert frontier is not None and frontier[1] == old_id


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
