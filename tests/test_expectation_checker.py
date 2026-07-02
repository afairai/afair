"""Expectation-checker tests — Phase 0.5 observability.

The regression target is the *silent-drop* failure mode: an
``event.written`` that entered extraction and never reached a terminal
stage (no completed, no failed) — invisible to logs and to the
extraction-retry worker. The checker must flag it as ``stuck_extractions``
and append a counters-only snapshot. Also covers the retry-exhausted /
permanent-failure counts, the append-only + counts-only snapshot
guarantees, and the ``pipeline_events.timeline`` read helper.

Old timestamps are rigged by raw INSERT with a backdated ISO
``recorded_at`` (not by monkeypatching time), mirroring how the real
pipeline records them.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from structlog.testing import capture_logs
from ulid import ULID

from afair.agents.expectation_checker import (
    EXTRACTION_GRACE_SECONDS,
    LOOKBACK_DAYS,
    ExpectationChecker,
)
from afair.agents.interpretation import write_failed_interpretation
from afair.agents.prompts import EXTRACTOR_SCHEMA_VERSION
from afair.settings import Settings
from afair.substrate import observability, open_db, write_event
from afair.substrate import pipeline_events as pe

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from afair.substrate.events import Event

PRODUCED_BY = "extractor:anthropic/claude-haiku-4-5"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def settings_local(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=tmp_path,
    )


# ── helpers ─────────────────────────────────────────────────────────────────


def _iso_ago(*, hours: float = 0, days: float = 0, seconds: float = 0) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours, days=days, seconds=seconds)).isoformat()


def _insert_pe(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    stage: str,
    recorded_at: str,
    status: str = pe.STATUS_OK,
    event_hash: str | None = None,
    producer: str | None = None,
) -> None:
    """Raw pipeline_events insert with an explicit (possibly backdated)
    ``recorded_at`` — the append-only helper always stamps ``now``."""
    with conn:
        conn.execute(
            """
            INSERT INTO pipeline_events
                (id, event_id, event_hash, stage, status, recorded_at, producer, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (str(ULID()), event_id, event_hash, stage, status, recorded_at, producer, None),
        )


def _write_event(conn: sqlite3.Connection, text: str) -> Event:
    return write_event(
        conn,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": text, "context": None, "type_hint": None},
    )


def _seed_failure(conn: sqlite3.Connection, event: Event, error_type: str) -> None:
    write_failed_interpretation(
        conn,
        event=event,
        version=EXTRACTOR_SCHEMA_VERSION,
        produced_by=PRODUCED_BY,
        error_type=error_type,
        error_message=f"seeded {error_type}",
    )


def _run(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    return ExpectationChecker().run(conn, settings)


# ── stuck / pending extraction (query 1, pipeline_events) ────────────────────


def test_flags_stuck_extraction(db: sqlite3.Connection, settings_local: Settings) -> None:
    """THE regression test: event.written + enqueued 1h ago, no terminal
    stage → flagged stuck, and the snapshot row carries the same count."""
    written_at = _iso_ago(hours=1)
    _insert_pe(db, event_id="01STUCK", stage=pe.STAGE_EVENT_WRITTEN, recorded_at=written_at)
    _insert_pe(db, event_id="01STUCK", stage=pe.STAGE_EXTRACTION_ENQUEUED, recorded_at=written_at)

    stats = _run(db, settings_local)
    assert stats["stuck_extractions"] == 1
    assert stats["pending_extraction_backlog"] == 0
    assert stats["expectation_violations"] == 1
    assert stats["oldest_stuck_age_seconds"] is not None
    assert stats["oldest_stuck_age_seconds"] >= EXTRACTION_GRACE_SECONDS

    snapshot = observability.read_latest_snapshot(db)
    assert snapshot is not None
    assert snapshot["counters"]["stuck_extractions"] == 1


def test_completed_extraction_not_flagged(db: sqlite3.Connection, settings_local: Settings) -> None:
    written_at = _iso_ago(hours=1)
    _insert_pe(db, event_id="01DONE", stage=pe.STAGE_EVENT_WRITTEN, recorded_at=written_at)
    _insert_pe(
        db, event_id="01DONE", stage=pe.STAGE_EXTRACTION_COMPLETED, recorded_at=_iso_ago(hours=1)
    )

    stats = _run(db, settings_local)
    assert stats["stuck_extractions"] == 0
    assert stats["pending_extraction_backlog"] == 0
    assert stats["oldest_stuck_age_seconds"] is None


def test_failed_extraction_not_stuck_but_counted(
    db: sqlite3.Connection, settings_local: Settings
) -> None:
    """A traced failure is NOT silent: extraction.failed is a terminal
    stage → not stuck. A single transient failure is below the retry cap
    → not retry_exhausted either."""
    written_at = _iso_ago(hours=1)
    event = _write_event(db, "timed out once, traced")
    _insert_pe(
        db,
        event_id=event.id,
        stage=pe.STAGE_EVENT_WRITTEN,
        recorded_at=written_at,
        event_hash=event.content_hash,
    )
    _insert_pe(
        db,
        event_id=event.id,
        stage=pe.STAGE_EXTRACTION_FAILED,
        recorded_at=_iso_ago(hours=1),
        status=pe.STATUS_FAILED,
        event_hash=event.content_hash,
    )
    _seed_failure(db, event, "llm_timeout")

    stats = _run(db, settings_local)
    assert stats["stuck_extractions"] == 0
    assert stats["retry_exhausted"] == 0
    assert stats["permanent_failures"] == 0
    assert stats["expectation_violations"] == 0


def test_recent_event_within_grace_not_flagged(
    db: sqlite3.Connection, settings_local: Settings
) -> None:
    """A just-written event is pending, not stuck — extraction hasn't had
    its grace window yet."""
    _insert_pe(
        db, event_id="01FRESH", stage=pe.STAGE_EVENT_WRITTEN, recorded_at=_iso_ago(seconds=5)
    )

    stats = _run(db, settings_local)
    assert stats["stuck_extractions"] == 0
    assert stats["pending_extraction_backlog"] == 1


def test_event_older_than_lookback_ignored(
    db: sqlite3.Connection, settings_local: Settings
) -> None:
    """The scan is bounded to LOOKBACK_DAYS — an ancient stuck row is a
    backfill concern, not live monitoring."""
    _insert_pe(
        db,
        event_id="01ANCIENT",
        stage=pe.STAGE_EVENT_WRITTEN,
        recorded_at=_iso_ago(days=LOOKBACK_DAYS + 23),
    )

    stats = _run(db, settings_local)
    assert stats["stuck_extractions"] == 0
    assert stats["pending_extraction_backlog"] == 0


# ── retry-exhausted / permanent failures (query 2, interpretations) ──────────


def test_retry_exhausted_counted(db: sqlite3.Connection, settings_local: Settings) -> None:
    """Three transient failures (llm_timeout) = retry cap reached →
    counted as retry_exhausted (extraction_retry has given up)."""
    event = _write_event(db, "keeps timing out")
    for _ in range(3):
        _seed_failure(db, event, "llm_timeout")

    stats = _run(db, settings_local)
    assert stats["retry_exhausted"] == 1
    assert stats["permanent_failures"] == 0
    assert stats["expectation_violations"] == 1


def test_permanent_failure_counted(db: sqlite3.Connection, settings_local: Settings) -> None:
    """A deterministic error (pdf_extraction_error) is permanent — awaits
    admin reprocess, never auto-retried."""
    event = _write_event(db, "unreadable pdf")
    _seed_failure(db, event, "pdf_extraction_error")

    stats = _run(db, settings_local)
    assert stats["permanent_failures"] == 1
    assert stats["retry_exhausted"] == 0
    # permanent failures are surfaced but not counted as a violation
    # (they need an operator, not automatic attention).
    assert stats["expectation_violations"] == 0


# ── snapshot guarantees ──────────────────────────────────────────────────────


def test_snapshot_written_even_when_zero(db: sqlite3.Connection, settings_local: Settings) -> None:
    """A clean cycle still writes a snapshot — its age is /health's
    checker-liveness signal."""
    _run(db, settings_local)
    snapshot = observability.read_latest_snapshot(db)
    assert snapshot is not None
    assert snapshot["counters"]["expectation_violations"] == 0
    assert snapshot["counters"]["lookback_days"] == LOOKBACK_DAYS


def test_snapshot_counts_only(db: sqlite3.Connection) -> None:
    """write_snapshot rejects non-int values — the code-level guarantee
    that no content/path can reach /health through a snapshot."""
    with pytest.raises(ValueError, match="must be int"):
        observability.write_snapshot(db, producer="test", counters={"bad": "a string"})  # type: ignore[dict-item]
    # None is allowed (nullable ages); ints are allowed.
    observability.write_snapshot(db, producer="test", counters={"a": 1, "b": None})


def test_snapshots_append_only(db: sqlite3.Connection) -> None:
    """I2: the snapshot table has the same no-UPDATE/no-DELETE triggers as
    pipeline_events."""
    observability.write_snapshot(db, producer="test", counters={"a": 1})
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE observability_snapshots SET producer = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM observability_snapshots")


# ── WARN log hygiene ─────────────────────────────────────────────────────────


def test_warn_carries_ids_not_content(db: sqlite3.Connection, settings_local: Settings) -> None:
    """The violation WARN must carry event IDs + integer counts only —
    never payloads, names, error strings, or paths."""
    _insert_pe(
        db, event_id="01STUCKID", stage=pe.STAGE_EVENT_WRITTEN, recorded_at=_iso_ago(hours=2)
    )

    with capture_logs() as logs:
        _run(db, settings_local)

    warns = [entry for entry in logs if entry.get("event") == "expectation_checker.violations"]
    assert len(warns) == 1
    entry = warns[0]
    assert "01STUCKID" in entry["sample_event_ids"]
    for key, value in entry.items():
        if key in ("event", "log_level"):
            assert isinstance(value, str)
        elif key == "sample_event_ids":
            assert all(isinstance(x, str) for x in value)
        else:
            # Every other structured field is an integer count.
            assert isinstance(value, int), f"{key}={value!r} is not an int"


def test_no_warn_when_clean(db: sqlite3.Connection, settings_local: Settings) -> None:
    """No violations → no WARN (avoid alert fatigue)."""
    with capture_logs() as logs:
        _run(db, settings_local)
    assert not [e for e in logs if e.get("event") == "expectation_checker.violations"]


# ── timeline read helper (deliverable A) ─────────────────────────────────────


def test_timeline_orders_stages(db: sqlite3.Connection) -> None:
    """pipeline_events.timeline returns stages oldest-first with a derived
    seconds_since_previous gap."""
    _insert_pe(db, event_id="01TL", stage=pe.STAGE_EVENT_WRITTEN, recorded_at=_iso_ago(seconds=120))
    _insert_pe(
        db, event_id="01TL", stage=pe.STAGE_EXTRACTION_STARTED, recorded_at=_iso_ago(seconds=90)
    )
    _insert_pe(
        db, event_id="01TL", stage=pe.STAGE_EXTRACTION_COMPLETED, recorded_at=_iso_ago(seconds=60)
    )

    tl = pe.timeline(db, "01TL")
    assert [row["stage"] for row in tl] == [
        pe.STAGE_EVENT_WRITTEN,
        pe.STAGE_EXTRACTION_STARTED,
        pe.STAGE_EXTRACTION_COMPLETED,
    ]
    assert tl[0]["seconds_since_previous"] is None
    # ~30s between each backdated stage.
    assert tl[1]["seconds_since_previous"] == pytest.approx(30, abs=2)
    assert tl[2]["seconds_since_previous"] == pytest.approx(30, abs=2)
