"""Salience worker + Mode-switching agent tests.

Together these implement Phase 2 cognitive routing. Salience scores
every event for "does this matter"; mode-switcher reads the rolling
salience signal and emits mode-transition observe events.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from afair.agents.mode_switcher import (
    MODE_CEN,
    MODE_DMN,
    MODE_SWITCHER_ORIGIN,
    SWITCH_TO_CEN_THRESHOLD,
    SWITCH_TO_DMN_THRESHOLD,
    ModeSwitcher,
    read_current_mode,
)
from afair.agents.salience import (
    HIGH_SIGNAL_TYPE_HINTS,
    SALIENCE_PRODUCED_BY,
    SalienceWorker,
    read_recent_salience,
    score_event,
)
from afair.settings import Settings
from afair.substrate import open_db, write_event

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
    )


# ── score_event ────────────────────────────────────────────────────────────


def test_score_event_baseline_recent_only(db: sqlite3.Connection) -> None:
    """A bare, recent text event scores low — only recency contributes."""
    event = write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "hi"},
    )
    score, components = score_event(db, event)
    # Recency ≈ 1.0; everything else = 0 → score ≈ 0.20 (W_RECENCY)
    assert 0.15 < score < 0.30
    assert components["recency"] > 0.99
    assert components["entity_density"] == 0
    assert components["has_conflict"] == 0
    assert components["is_compound"] == 0


def test_score_event_type_hint_bump(db: sqlite3.Connection) -> None:
    """A high-signal type_hint adds the W_TYPE_HINT_BUMP weight."""
    for hint in HIGH_SIGNAL_TYPE_HINTS:
        event = write_event(
            db,
            origin="user",
            kind="remember",
            payload={"content_type": "text", "text": f"hi {hint}", "type_hint": hint},
        )
        score, components = score_event(db, event)
        assert components["type_hint_bump"] == 1.0, hint
        # type_hint adds ≥0.15; bare event was ~0.20 → bumped event ≥0.35
        # (small float tolerance for the recency-decay tick during the test)
        assert score >= 0.34, (hint, score)


def test_score_event_compound_bump(db: sqlite3.Connection) -> None:
    event = write_event(
        db,
        origin="user",
        kind="remember",
        payload={
            "content_type": "compound",
            "parts": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
        },
    )
    _, components = score_event(db, event)
    assert components["is_compound"] == 1.0


def test_score_event_recency_decays(db: sqlite3.Connection) -> None:
    """An old event has 0.0 recency contribution."""
    event = write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "old"},
    )
    # Force the event's created_at to 60 days ago — bypasses the I2
    # trigger by writing to a sibling temp table for the date math.
    old = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    # We can't UPDATE events (I2), so we score with a synthetic event
    # carrying the old timestamp.
    from types import SimpleNamespace

    synthetic = SimpleNamespace(
        content_hash=event.content_hash,
        payload=event.payload,
        created_at=old,
    )
    score, components = score_event(db, synthetic)
    assert components["recency"] == 0.0
    # With only recency contribution gone, a bare event scores 0.
    assert score == 0.0


# ── SalienceWorker.run ─────────────────────────────────────────────────────


def test_salience_worker_scores_unscored_events(db: sqlite3.Connection, settings: Settings) -> None:
    for i in range(3):
        write_event(
            db,
            origin="user",
            kind="remember",
            payload={"content_type": "text", "text": f"event {i}"},
        )
    worker = SalienceWorker()
    stats = worker.run(db, settings)
    assert stats["candidates"] == 3
    assert stats["scored"] == 3

    # Re-running is idempotent — no new scores written.
    stats2 = worker.run(db, settings)
    assert stats2["candidates"] == 0
    assert stats2["scored"] == 0


def test_salience_worker_skips_consolidations(db: sqlite3.Connection, settings: Settings) -> None:
    """Only remember + observe events are candidates; consolidations
    and invalidations are derived and shouldn't be scored."""
    write_event(
        db,
        origin="agent:consolidator",
        kind="consolidate",
        payload={"content_type": "text", "text": "roll-up"},
    )
    write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "real event"},
    )
    stats = SalienceWorker().run(db, settings)
    assert stats["scored"] == 1  # only the remember


def test_read_recent_salience_orders_most_recent_first(
    db: sqlite3.Connection, settings: Settings
) -> None:
    for i in range(5):
        write_event(
            db,
            origin="user",
            kind="remember",
            payload={"content_type": "text", "text": f"event {i}"},
        )
    SalienceWorker().run(db, settings)
    recent = read_recent_salience(db, limit=3)
    assert len(recent) == 3
    timestamps = [row[2] for row in recent]
    assert timestamps == sorted(timestamps, reverse=True)


# ── ModeSwitcher ───────────────────────────────────────────────────────────


def test_read_current_mode_defaults_to_dmn(db: sqlite3.Connection) -> None:
    """Clean vault → DMN (no transition events yet)."""
    assert read_current_mode(db) == MODE_DMN


def test_mode_switcher_does_nothing_when_no_salience(
    db: sqlite3.Connection, settings: Settings
) -> None:
    stats = ModeSwitcher().run(db, settings)
    assert stats["transitioned"] is False
    assert stats["current_mode"] is None  # no salience to compute against


def _seed_high_salience(db: sqlite3.Connection, settings: Settings, *, n: int = 20) -> None:
    """Write n high-salience events so the cumulative score exceeds
    SWITCH_TO_CEN_THRESHOLD. Each event uses a high-signal type_hint."""
    for i in range(n):
        write_event(
            db,
            origin="user",
            kind="remember",
            payload={
                "content_type": "text",
                "text": f"decision {i}",
                "type_hint": "decision",
            },
        )
    SalienceWorker().run(db, settings)


def test_mode_switcher_transitions_dmn_to_cen(db: sqlite3.Connection, settings: Settings) -> None:
    # 20 compound + decision-hint events. Each scores
    # recency(≈1.0)*0.20 + type_hint(1.0)*0.15 + is_compound(1.0)*0.10
    # = ≈0.45, summing to ≈9.0 across the window — well above 8.0.
    for i in range(20):
        write_event(
            db,
            origin="user",
            kind="remember",
            payload={
                "content_type": "compound",
                "parts": [{"type": "text", "text": f"compound {i}"}],
                "type_hint": "decision",
            },
        )
    SalienceWorker().run(db, settings)

    stats = ModeSwitcher().run(db, settings)
    assert stats["cumulative_salience"] >= SWITCH_TO_CEN_THRESHOLD
    assert stats["transitioned"] is True
    assert stats["to_mode"] == MODE_CEN
    # Verify the observe event landed.
    assert read_current_mode(db) == MODE_CEN


def test_mode_switcher_does_not_flap_on_borderline(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """Running the switcher twice in succession with the same data
    transitions once and then sits still."""
    # Same compound + type_hint shape as the dmn→cen test so we
    # cross the threshold reliably.
    for i in range(20):
        write_event(
            db,
            origin="user",
            kind="remember",
            payload={
                "content_type": "compound",
                "parts": [{"type": "text", "text": f"c{i}"}],
                "type_hint": "decision",
            },
        )
    SalienceWorker().run(db, settings)
    s1 = ModeSwitcher().run(db, settings)
    s2 = ModeSwitcher().run(db, settings)
    assert s1["transitioned"] is True
    assert s2["transitioned"] is False  # already in CEN
    assert s2["current_mode"] == MODE_CEN


def test_mode_switcher_returns_to_dmn_when_quiet(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """Boot to CEN by hand, then add low-salience events; the next
    cycle should transition back to DMN."""
    # Directly seed a CEN transition event so we start in CEN.
    write_event(
        db,
        origin=MODE_SWITCHER_ORIGIN,
        kind="observe",
        payload={
            "content_type": "event",
            "action": "mode_switched",
            "subject": MODE_CEN,
            "result": "test seed",
        },
    )
    assert read_current_mode(db) == MODE_CEN

    # Now add 20 quiet text events (bare recency only).
    for i in range(20):
        write_event(
            db,
            origin="user",
            kind="remember",
            payload={"content_type": "text", "text": f"q{i}"},
        )
    SalienceWorker().run(db, settings)
    stats = ModeSwitcher().run(db, settings)
    # Bare events ~0.20 each, 20 of them sum to ~4.0;
    # ≤ SWITCH_TO_DMN_THRESHOLD (4.0)
    # → transition back to DMN
    assert stats["cumulative_salience"] <= SWITCH_TO_DMN_THRESHOLD + 0.5
    assert stats["to_mode"] == MODE_DMN
    assert read_current_mode(db) == MODE_DMN
    # Mode transition event carries the diagnostic cumulative_salience.
    row = db.execute(
        "SELECT payload FROM events WHERE origin = ? ORDER BY created_at DESC LIMIT 1",
        (MODE_SWITCHER_ORIGIN,),
    ).fetchone()
    payload = json.loads(row["payload"])
    assert "cumulative_salience" in payload
    assert payload["window_size"] == 20


def test_salience_interpretation_idempotent(db: sqlite3.Connection, settings: Settings) -> None:
    """Re-running the worker on the same vault writes no new
    interpretations — score is bound to (event_hash, salience:v0)."""
    write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "x"},
    )
    SalienceWorker().run(db, settings)
    SalienceWorker().run(db, settings)
    SalienceWorker().run(db, settings)
    count = db.execute(
        "SELECT COUNT(*) FROM interpretations WHERE produced_by = ?",
        (SALIENCE_PRODUCED_BY,),
    ).fetchone()[0]
    assert count == 1
