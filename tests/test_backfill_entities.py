"""Phase 4 Track 1 Stage 5 — backfill script tests."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from neverforget.agents import entity_canonicalizer as ec
from neverforget.agents.interpretation import write_interpretation
from neverforget.substrate import open_db, write_event
from scripts.backfill_entities import main

if TYPE_CHECKING:
    from pathlib import Path


def _seed_event_with_entities(db: Any, *, text: str, entities: list[dict[str, str]]) -> None:
    event = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": text}
    )
    write_interpretation(
        db,
        event=event,
        version=1,
        produced_by="extractor:anthropic/claude-haiku-4-5",
        extraction={
            "status": "success",
            "best_guess_kind": "fact",
            "summary": text[:200],
            "entities": entities,
            "relations": [],
        },
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ec, "_maybe_sleep", lambda _last: 0.0)


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backfill should never need the LLM in tests — exact matches only."""

    def _boom(**_: Any) -> Any:
        msg = "test backfill should not call LLM"
        raise AssertionError(msg)

    monkeypatch.setattr(ec, "call_tool", _boom)


def test_backfill_processes_existing_events(tmp_path: Path) -> None:
    """A vault with 3 events whose extractor ran but canonicalizer didn't
    should end up with 3 canonical entities (one per unique surface form)."""
    db = open_db(tmp_path)
    _seed_event_with_entities(
        db, text="Sajinth runs Athara", entities=[{"name": "Sajinth", "type": "person"}]
    )
    _seed_event_with_entities(
        db, text="Sajinth shipped a fix", entities=[{"name": "Sajinth", "type": "person"}]
    )
    _seed_event_with_entities(
        db, text="elvah project status update", entities=[{"name": "elvah", "type": "organization"}]
    )
    db.close()

    rc = main(["--vault-dir", str(tmp_path), "--quiet"])
    assert rc == 0

    db2 = open_db(tmp_path)
    try:
        canonical_count = db2.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"]
        mention_count = db2.execute("SELECT COUNT(*) AS n FROM entity_mentions").fetchone()["n"]
    finally:
        db2.close()
    # 2 unique canonicals: "Sajinth" (person) and "elvah" (organization).
    assert canonical_count == 2
    # 3 mentions: one per event.
    assert mention_count == 3


def test_backfill_is_idempotent(tmp_path: Path) -> None:
    """Running twice produces no extra entities or mentions."""
    db = open_db(tmp_path)
    _seed_event_with_entities(
        db, text="Sajinth runs Athara", entities=[{"name": "Sajinth", "type": "person"}]
    )
    db.close()

    main(["--vault-dir", str(tmp_path), "--quiet"])
    main(["--vault-dir", str(tmp_path), "--quiet"])

    db2 = open_db(tmp_path)
    try:
        assert db2.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"] == 1
        assert db2.execute("SELECT COUNT(*) AS n FROM entity_mentions").fetchone()["n"] == 1
    finally:
        db2.close()


def test_backfill_writes_observe_event_for_audit(tmp_path: Path) -> None:
    """I7: the backfill itself is recorded as a substrate event."""
    db = open_db(tmp_path)
    _seed_event_with_entities(
        db, text="Sajinth runs Athara", entities=[{"name": "Sajinth", "type": "person"}]
    )
    db.close()

    main(["--vault-dir", str(tmp_path), "--quiet"])

    db2 = open_db(tmp_path)
    try:
        rows = db2.execute(
            "SELECT payload FROM events WHERE kind = 'observe' "
            "AND json_extract(payload, '$.action') = 'backfill_entities'"
        ).fetchall()
    finally:
        db2.close()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["subject"] == "phase4_track1_stage5"
    assert payload["result"] == "completed"
    assert "cycles_run" in payload
    assert "duration_seconds" in payload
    # Stats should be present with reasonable values.
    assert payload.get("events_canonicalized", 0) >= 1


def test_backfill_returns_nonzero_when_vault_missing(tmp_path: Path) -> None:
    """No substrate.db → exit code 2, no observe event written."""
    rc = main(["--vault-dir", str(tmp_path / "nonexistent"), "--quiet"])
    assert rc == 2


def test_backfill_max_cycles_bounds_runaway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If something went wrong and the worker reported work-done forever,
    --max-cycles would still cap the loop."""
    db = open_db(tmp_path)
    _seed_event_with_entities(
        db, text="Sajinth runs Athara", entities=[{"name": "Sajinth", "type": "person"}]
    )
    db.close()

    # Force the worker to claim it did work every cycle.
    call_count = {"n": 0}

    real_run = ec.EntityCanonicalizer.run

    def _fake_run(self: ec.EntityCanonicalizer, conn: Any, settings: Any) -> dict[str, Any]:
        call_count["n"] += 1
        stats = real_run(self, conn, settings)
        # Lie: claim we did work even when we didn't, to test the cycle cap.
        stats["events_canonicalized"] = 1
        return stats

    monkeypatch.setattr(ec.EntityCanonicalizer, "run", _fake_run)

    main(["--vault-dir", str(tmp_path), "--max-cycles", "3", "--quiet"])
    assert call_count["n"] == 3
