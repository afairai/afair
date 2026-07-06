"""ADR-0003 Phase 2 completion — Slice 2: budget-exhaustion defer (G1).

Before this slice, an exhausted per-cycle LLM budget drained the remaining
events EXACT-ONLY, which re-opened the residual formation path: a kind flip
on an existing name with no LLM available minted a NEW same-name cross-kind
v2 duplicate. Now the cycle DEFERS the remaining events (zero mentions →
re-surfaced next cycle with a fresh budget). The within-event fallback
(plain create on mid-event exhaustion) is kept — it never loses a mention.

Regression: `test_budget_zero_defers_instead_of_creating` is red before the
slice (exact-only would have created the duplicate) and green after.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from afair.agents import entity_canonicalizer as ec
from afair.agents.entity_canonicalizer import EntityCanonicalizer
from afair.agents.interpretation import write_interpretation
from afair.agents.llm import LLMResult
from afair.settings import Settings
from afair.substrate import open_db, write_entity, write_event

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
        cold_path_enabled=False,
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ec, "_maybe_sleep", lambda _last: 0.0)


def _event_with_entities(
    conn: sqlite3.Connection, *, text: str, entities: list[dict[str, str]]
) -> str:
    event = write_event(
        conn, origin="user", kind="remember", payload={"content_type": "text", "text": text}
    )
    write_interpretation(
        conn,
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
    return event.content_hash


def _mention_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM entity_mentions").fetchone()["n"])


def _entity_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"])


def test_budget_zero_defers_instead_of_creating(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (G1): with the budget gone, events are deferred, not drained.

    A pre-existing "Apple" org plus a new "Apple" product mention. Exact-only
    drain would mint a cross-kind duplicate; deferral leaves the graph
    untouched until a cycle with budget can judge the homonym.
    """
    write_entity(
        db,
        canonical_name="Apple",
        kind="organization",
        created_by="test",
        source_event_id=write_event(
            db, origin="user", kind="remember", payload={"content_type": "text", "text": "seed"}
        ).id,
        confidence=0.9,
    )
    before_entities = _entity_count(db)

    _event_with_entities(
        db, text="Apple product launch", entities=[{"name": "Apple", "type": "product"}]
    )
    _event_with_entities(
        db, text="Banana product launch", entities=[{"name": "Banana", "type": "product"}]
    )

    def _no_llm(**_: Any) -> LLMResult:
        msg = "no LLM call expected when the budget is zero"
        raise AssertionError(msg)

    monkeypatch.setattr(ec, "call_tool", _no_llm)
    monkeypatch.setattr(ec, "MAX_LLM_CALLS_PER_CYCLE", 0)

    stats = EntityCanonicalizer().run(db, settings)

    assert stats["events_deferred_no_budget"] == 2
    assert stats["events_canonicalized"] == 0
    assert stats["entities_created"] == 0
    assert _entity_count(db) == before_entities  # no duplicate minted
    assert _mention_count(db) == 0  # deferred events keep zero mentions


def test_deferral_blocks_watermark_advance(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2a drain-blocker: a cycle that DEFERS any event (LLM budget gone) must
    NOT advance the watermark — the deferred events are still candidates and
    would be skipped next cycle if the cursor jumped past them."""
    from afair.substrate import watermarks

    # Disable the lag so a clean cycle *would* advance — isolating the deferral
    # as the reason it doesn't here.
    monkeypatch.setattr(watermarks, "FRONTIER_LAG_SECONDS", -3600)
    _event_with_entities(
        db, text="Cherry product launch", entities=[{"name": "Cherry", "type": "product"}]
    )
    monkeypatch.setattr(ec, "MAX_LLM_CALLS_PER_CYCLE", 0)

    stats = EntityCanonicalizer().run(db, settings)
    assert stats["events_deferred_no_budget"] == 1
    assert watermarks.read_watermark_id(db, watermarks.WORKER_CANONICALIZER) is None


def test_deferred_events_process_on_next_cycle_with_budget(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The zero-mention events re-surface and are handled once budget returns."""
    # Distinct names AND distinct kinds → no shared candidate pool, so
    # Stage-3 create runs with no LLM on the second cycle.
    _event_with_entities(db, text="Zeta ships", entities=[{"name": "Zeta", "type": "product"}])
    _event_with_entities(
        db, text="Yotta is an idea", entities=[{"name": "Yotta", "type": "concept"}]
    )

    # Cycle 1: no budget → defer everything.
    monkeypatch.setattr(ec, "MAX_LLM_CALLS_PER_CYCLE", 0)
    stats1 = EntityCanonicalizer().run(db, settings)
    assert stats1["events_deferred_no_budget"] == 2
    assert _entity_count(db) == 0

    # Cycle 2: budget restored. Distinct new names → Stage-3 create, no LLM.
    def _no_llm(**_: Any) -> LLMResult:
        msg = "distinct new names should not need the LLM"
        raise AssertionError(msg)

    monkeypatch.setattr(ec, "call_tool", _no_llm)
    monkeypatch.setattr(ec, "MAX_LLM_CALLS_PER_CYCLE", 8)
    stats2 = EntityCanonicalizer().run(db, settings)

    assert stats2["events_deferred_no_budget"] == 0
    assert stats2["events_canonicalized"] == 2
    assert stats2["entities_created"] == 2
    assert _entity_count(db) == 2


def test_mid_event_exhaustion_still_completes_event(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kept path: when the budget hits zero PART WAY through an event, the
    remaining entities in that same event still resolve via the plain-create
    fallback — a mention is never lost."""
    seed_event = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": "seed"}
    )
    for name in ("Apple", "Banana"):
        write_entity(
            db,
            canonical_name=name,
            kind="organization",
            created_by="test",
            source_event_id=seed_event.id,
            confidence=0.9,
        )

    # One event mentioning both, each a cross-kind homonym question.
    h = _event_with_entities(
        db,
        text="Apple product and Banana product shipped",
        entities=[
            {"name": "Apple", "type": "product"},
            {"name": "Banana", "type": "product"},
        ],
    )

    calls = {"n": 0}

    def _llm_none(**_: Any) -> LLMResult:
        calls["n"] += 1
        return LLMResult(
            data={"matched_entity_id": None, "reason": "homonym", "confidence": 0.95},
            model="test",
            raw="",
        )

    monkeypatch.setattr(ec, "call_tool", _llm_none)
    monkeypatch.setattr(ec, "MAX_LLM_CALLS_PER_CYCLE", 1)

    stats = EntityCanonicalizer().run(db, settings)

    # Only one LLM call fit in the budget; the second entity used the
    # within-event fallback — but BOTH still got a mention in this event.
    assert calls["n"] == 1
    assert stats["events_deferred_no_budget"] == 0
    assert stats["events_canonicalized"] == 1
    assert stats["entities_created"] == 2
    mentions = db.execute(
        "SELECT surface_form FROM entity_mentions WHERE event_hash = ?", (h,)
    ).fetchall()
    assert {m["surface_form"] for m in mentions} == {"Apple", "Banana"}


def test_defer_stat_present_in_normal_cycle(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new stat is always present, zero when nothing is deferred."""
    _event_with_entities(db, text="Omega ships", entities=[{"name": "Omega", "type": "product"}])

    def _no_llm(**_: Any) -> LLMResult:
        msg = "no LLM needed"
        raise AssertionError(msg)

    monkeypatch.setattr(ec, "call_tool", _no_llm)
    stats = EntityCanonicalizer().run(db, settings)
    assert "events_deferred_no_budget" in stats
    assert stats["events_deferred_no_budget"] == 0
