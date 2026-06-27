"""Tests for the afair://session-start MCP resource.

Verifies the payload shape, the salience-ranked event surfacing, the
open_threads pull from the consolidator, and the cache semantics.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from ulid import ULID

from afair.agents.mode_switcher import MODE_CEN, MODE_DMN, MODE_SWITCHER_ORIGIN
from afair.agents.salience import SalienceWorker
from afair.mcp import resources
from afair.settings import Settings
from afair.substrate import open_db, write_event, write_event_temporal

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


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    resources.clear_cache()


# ── payload shape ─────────────────────────────────────────────────────────


def test_empty_vault_yields_default_payload(db: sqlite3.Connection) -> None:
    """Fresh vault: no salient events, no threads, mode = DMN."""
    payload = resources.read_session_start(db)
    assert payload["mode"] == MODE_DMN
    assert payload["recent_salient_events"] == []
    assert payload["open_threads"] == []
    assert payload["vault_size"] == {"total": 0, "remembers": 0, "observes": 0}
    assert payload["cumulative_salience"] == 0.0
    assert payload["pending_corrections"] == []
    assert "instructions" in payload


def test_pending_corrections_surface_with_prompt(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """An open audit proposal shows up at session start with a yes/no prompt
    and decide-instructions, so the AI can raise it proactively."""
    from afair.agents.entity_audit import EntityAuditWorker
    from afair.substrate import write_entity

    ev = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": "maxime.team"}
    )
    write_entity(
        db,
        canonical_name="maxime.team",
        kind="person",
        created_by="t",
        source_event_id=ev.id,
        confidence=0.8,
    )
    EntityAuditWorker().run(db, settings)

    payload = resources.build_session_start_payload(db)
    pending = payload["pending_corrections"]
    assert len(pending) == 1
    assert pending[0]["kind"] == "retype"
    assert "re-type" in pending[0]["prompt"]
    assert "afair.recall(decide=" in payload["instructions"]


def test_salient_events_surface_top_n_by_score(db: sqlite3.Connection, settings: Settings) -> None:
    """Top-salience events appear, ordered most-salient first."""
    # Mix: 3 high-signal (decision + compound), 2 plain texts.
    for i in range(3):
        write_event(
            db,
            origin="user",
            kind="remember",
            payload={
                "content_type": "compound",
                "parts": [{"type": "text", "text": f"decision part {i}"}],
                "type_hint": "decision",
            },
        )
    for i in range(2):
        write_event(
            db,
            origin="user",
            kind="remember",
            payload={"content_type": "text", "text": f"casual note {i}"},
        )
    SalienceWorker().run(db, settings)

    payload = resources.read_session_start(db)
    events = payload["recent_salient_events"]
    assert len(events) == 5
    # Score-ordered descending.
    scores = [e["salience"] for e in events]
    assert scores == sorted(scores, reverse=True)
    # High-signal events score higher than plain.
    high_signal = [e for e in events if e.get("type_hint") == "decision"]
    casual = [e for e in events if e.get("type_hint") != "decision"]
    assert all(h["salience"] > c["salience"] for h in high_signal for c in casual)


def test_recent_salient_capped_at_limit(db: sqlite3.Connection, settings: Settings) -> None:
    """Even with 20 scored events, the resource caps at _SALIENT_LIMIT (10)."""
    for i in range(20):
        write_event(
            db,
            origin="user",
            kind="remember",
            payload={"content_type": "text", "text": f"event {i}"},
        )
    SalienceWorker().run(db, settings)

    payload = resources.read_session_start(db)
    assert len(payload["recent_salient_events"]) <= 10


def test_event_preview_truncates_long_text(db: sqlite3.Connection, settings: Settings) -> None:
    long_text = "x" * 500
    write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": long_text},
    )
    SalienceWorker().run(db, settings)
    payload = resources.read_session_start(db)
    assert len(payload["recent_salient_events"][0]["preview"]) <= 200


def test_compound_event_preview_uses_first_text_part(
    db: sqlite3.Connection, settings: Settings
) -> None:
    write_event(
        db,
        origin="user",
        kind="remember",
        payload={
            "content_type": "compound",
            "parts": [
                {"type": "text", "text": "transcript content here"},
                {"type": "blob-ref", "blob_hash": "sha256:" + "0" * 64, "mime": "image/png"},
            ],
        },
    )
    SalienceWorker().run(db, settings)
    payload = resources.read_session_start(db)
    preview = payload["recent_salient_events"][0]["preview"]
    assert "transcript content" in preview


def test_observe_event_preview_renders_action_subject(
    db: sqlite3.Connection, settings: Settings
) -> None:
    write_event(
        db,
        origin="agent",
        kind="observe",
        payload={
            "content_type": "event",
            "action": "edit_file",
            "subject": "events.py",
            "result": "added inline-vs-spill logic",
        },
    )
    SalienceWorker().run(db, settings)
    payload = resources.read_session_start(db)
    preview = payload["recent_salient_events"][0]["preview"]
    assert "edit_file" in preview and "events.py" in preview


# ── mode + cumulative salience ────────────────────────────────────────────


def test_mode_reflects_substrate_state(db: sqlite3.Connection) -> None:
    # Seed a CEN-transition event by hand.
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
    payload = resources.read_session_start(db)
    assert payload["mode"] == MODE_CEN


def test_cumulative_salience_sums_surfaced_events(
    db: sqlite3.Connection, settings: Settings
) -> None:
    for i in range(3):
        write_event(
            db,
            origin="user",
            kind="remember",
            payload={"content_type": "text", "text": f"event {i}"},
        )
    SalienceWorker().run(db, settings)
    payload = resources.read_session_start(db)
    expected = sum(e["salience"] for e in payload["recent_salient_events"])
    assert payload["cumulative_salience"] == pytest.approx(expected, rel=0.01)


# ── open_threads from consolidator ────────────────────────────────────────


def _seed_consolidation(db: sqlite3.Connection, threads: list[str]) -> None:
    """Write a synthetic consolidation interpretation row."""
    extraction = {
        "status": "success",
        "content_type": "daily_consolidation",
        "themes": ["test theme"],
        "open_threads": threads,
    }
    # Need a source event to anchor the interpretation.
    event = write_event(
        db,
        origin="agent:consolidator",
        kind="consolidate",
        payload={"content_type": "text", "text": "synthetic consolidation"},
    )
    db.execute(
        """
        INSERT INTO interpretations (
            id, event_id, event_hash, version, produced_at,
            produced_by, extraction
        ) VALUES (?, ?, ?, 0, ?, 'consolidator:v0:test', ?)
        """,
        (
            str(ULID()),
            event.id,
            event.content_hash,
            datetime.now(UTC).isoformat(),
            json.dumps(extraction),
        ),
    )
    db.commit()


def test_open_threads_surfaces_consolidator_output(db: sqlite3.Connection) -> None:
    _seed_consolidation(db, ["finish Stripe webhook", "rotate prod tokens"])
    payload = resources.read_session_start(db)
    assert "finish Stripe webhook" in payload["open_threads"]
    assert "rotate prod tokens" in payload["open_threads"]


def test_open_threads_handles_dict_format(db: sqlite3.Connection) -> None:
    """Some consolidator versions emit {text: ...} shaped threads."""
    extraction = {
        "status": "success",
        "open_threads": [
            {"text": "merge the docs branch"},
            {"text": "ping support@"},
        ],
    }
    event = write_event(
        db,
        origin="agent:consolidator",
        kind="consolidate",
        payload={"content_type": "text", "text": "synthetic"},
    )
    db.execute(
        """
        INSERT INTO interpretations (id, event_id, event_hash, version,
            produced_at, produced_by, extraction)
        VALUES (?, ?, ?, 0, ?, 'consolidator:v0:test2', ?)
        """,
        (
            str(ULID()),
            event.id,
            event.content_hash,
            datetime.now(UTC).isoformat(),
            json.dumps(extraction),
        ),
    )
    db.commit()
    payload = resources.read_session_start(db)
    assert "merge the docs branch" in payload["open_threads"]


def test_open_threads_capped_at_limit(db: sqlite3.Connection) -> None:
    threads = [f"thread {i}" for i in range(20)]
    _seed_consolidation(db, threads)
    payload = resources.read_session_start(db)
    assert len(payload["open_threads"]) <= 8


# ── cache ─────────────────────────────────────────────────────────────────


def test_cache_returns_same_payload_on_repeated_read(
    db: sqlite3.Connection, settings: Settings
) -> None:
    write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "event"},
    )
    SalienceWorker().run(db, settings)
    a = resources.read_session_start(db)
    b = resources.read_session_start(db)
    assert a == b


def test_cache_invalidates_on_new_event(db: sqlite3.Connection, settings: Settings) -> None:
    """A new remember after a session-start read returns fresh data."""
    write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "event 1"},
    )
    SalienceWorker().run(db, settings)
    payload_before = resources.read_session_start(db)
    assert payload_before["vault_size"]["total"] == 1

    write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "event 2"},
    )
    # Cache key uses latest event id — the new event invalidates implicitly.
    payload_after = resources.read_session_start(db)
    assert payload_after["vault_size"]["total"] == 2


def test_session_start_includes_upcoming(db: sqlite3.Connection) -> None:
    """A recurring memory coming due soon surfaces in the upcoming field."""
    event = write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "Mara's birthday"},
    )
    soon = (datetime.now(UTC) + timedelta(days=4)).isoformat()
    write_event_temporal(
        db,
        event_id=event.id,
        event_hash=event.content_hash,
        temporal_class="recurring",
        confidence=0.9,
        computed_by="temporal:v1",
        event_time=soon,
        recurrence_rule="FREQ=YEARLY",
    )
    payload = resources.build_session_start_payload(db)
    assert "upcoming" in payload
    ids = {item["event_id"] for item in payload["upcoming"]}
    assert event.id in ids
    item = next(i for i in payload["upcoming"] if i["event_id"] == event.id)
    assert item["temporal_class"] == "recurring"
    assert item["when"] is not None
    assert "upcoming" in payload["instructions"]


def test_session_start_upcoming_empty_when_nothing_due(db: sqlite3.Connection) -> None:
    write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "a plain note"},
    )
    payload = resources.build_session_start_payload(db)
    assert payload["upcoming"] == []
