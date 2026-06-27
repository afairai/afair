"""Recall-side tests for temporal decay (relevance-decay Phase 2).

The ``_temporal_rerank`` helper in isolation, plus the wiring through
``handlers.recall``: default recall de-prioritizes a decayed one-off, and
``depth="deep"`` (the flat history lens) bypasses the decay entirely.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from afair.mcp import handlers
from afair.mcp.context import ServerContext, clear_context, set_context
from afair.mcp.schemas import TextContent
from afair.substrate import open_db, write_event, write_event_temporal

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


# A relevance horizon well in the past, so a one-off classed against it decays.
_PAST_HORIZON = "2025-01-01T00:00:00+00:00"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _disable_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("afair.mcp.handlers.schedule_extraction", lambda _event_id: None)


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[ServerContext]:
    conn = open_db(tmp_path)
    sc = ServerContext(
        db=conn,
        vault_dir=tmp_path,
        inline_text_max_bytes=64 * 1024,
        semantic_recall_enabled=False,
    )
    set_context(sc)
    try:
        yield sc
    finally:
        conn.close()
        clear_context()


# ── _temporal_rerank in isolation ────────────────────────────────────────────


def test_temporal_rerank_sinks_a_decayed_one_off(db: sqlite3.Connection) -> None:
    evergreen = write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "I take my coffee black"},
    )
    decayed = write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "deadline for the Q1 report"},
    )
    write_event_temporal(
        db,
        event_id=evergreen.id,
        event_hash=evergreen.content_hash,
        temporal_class="evergreen",
        confidence=0.9,
        computed_by="temporal:v1",
    )
    write_event_temporal(
        db,
        event_id=decayed.id,
        event_hash=decayed.content_hash,
        temporal_class="one_off",
        confidence=1.0,
        computed_by="temporal:v1",
        relevance_horizon=_PAST_HORIZON,
    )
    # Incoming order puts the decayed one-off FIRST; the rerank must sink it.
    out = handlers._temporal_rerank([decayed, evergreen], db)
    assert [e.id for e in out] == [evergreen.id, decayed.id]


def test_temporal_rerank_is_a_noop_without_records(db: sqlite3.Connection) -> None:
    a = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": "a"}
    )
    b = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": "b"}
    )
    out = handlers._temporal_rerank([a, b], db)
    assert [e.id for e in out] == [a.id, b.id]  # untouched


# ── wiring through handlers.recall ───────────────────────────────────────────


def _seed_pair(ctx: ServerContext) -> tuple[str, str]:
    """An evergreen hit and a decayed one-off, both matching 'aurora'."""
    ever = handlers.remember(
        content=TextContent(type="text", text="aurora is my daughter's name"),
        context="t",
    )
    decayed = handlers.remember(
        content=TextContent(type="text", text="aurora project deadline has passed"),
        context="t",
    )
    write_event_temporal(
        ctx.db,
        event_id=ever.event_id,
        event_hash=ever.content_hash,
        temporal_class="evergreen",
        confidence=0.9,
        computed_by="temporal:v1",
    )
    write_event_temporal(
        ctx.db,
        event_id=decayed.event_id,
        event_hash=decayed.content_hash,
        temporal_class="one_off",
        confidence=1.0,
        computed_by="temporal:v1",
        relevance_horizon=_PAST_HORIZON,
    )
    return ever.content_hash, decayed.content_hash


def test_default_recall_demotes_the_decayed_one_off(ctx: ServerContext) -> None:
    evergreen_hash, decayed_hash = _seed_pair(ctx)
    result = handlers.recall(query="aurora")
    hashes = [h.content_hash for h in result.hits]
    assert evergreen_hash in hashes
    assert decayed_hash in hashes
    assert hashes.index(evergreen_hash) < hashes.index(decayed_hash)


def test_default_recall_surfaces_temporal_relevance(ctx: ServerContext) -> None:
    _evergreen_hash, decayed_hash = _seed_pair(ctx)
    result = handlers.recall(query="aurora")
    decayed = next(h for h in result.hits if h.content_hash == decayed_hash)
    assert decayed.interpretation is not None
    assert decayed.interpretation["temporal_class"] == "one_off"
    assert decayed.interpretation["temporal_relevance"] < 1.0


def test_deep_depth_bypasses_temporal_rerank(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_pair(ctx)
    calls: list[int] = []
    original = handlers._temporal_rerank
    monkeypatch.setattr(
        handlers,
        "_temporal_rerank",
        lambda events, db: (calls.append(1), original(events, db))[1],
    )
    handlers.recall(query="aurora", depth="deep")
    assert calls == []  # the flat history lens never decays

    handlers.recall(query="aurora")  # default does
    assert calls == [1]
