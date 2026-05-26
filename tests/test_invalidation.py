"""Bi-temporal invalidation tests — Phase 2 (Graphiti-style supersession)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from afair.agents.invalidation import (
    INVALIDATE_KIND,
    InvalidationInfo,
    read_invalidation,
    read_invalidations_batch,
    write_invalidation,
)
from afair.mcp import handlers
from afair.mcp.context import ServerContext, clear_context, set_context
from afair.mcp.handlers import InvalidateTargetError
from afair.mcp.schemas import TextContent
from afair.substrate import open_db, write_event

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[ServerContext]:
    db = open_db(tmp_path)
    sc = ServerContext(
        db=db,
        vault_dir=tmp_path,
        inline_text_max_bytes=64 * 1024,
        semantic_recall_enabled=False,  # tests don't hit the embedding API
    )
    set_context(sc)
    try:
        yield sc
    finally:
        db.close()
        clear_context()


@pytest.fixture(autouse=True)
def _disable_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("afair.mcp.handlers.schedule_extraction", lambda _id: None)


# ── substrate layer ────────────────────────────────────────────────────────


def test_write_invalidation_creates_append_only_event(ctx: ServerContext) -> None:
    """The original target event is NOT touched (I2). A new event with
    kind='invalidate' is appended."""
    original = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "Sajinth is CEO"},
    )
    inv = write_invalidation(
        ctx.db,
        target_hash=original.content_hash,
        reason="he stepped down 2026-05-01",
        origin="agent",
    )
    assert inv.kind == "invalidate"
    assert inv.parent_hashes == [original.content_hash]

    # Both events coexist — substrate is append-only.
    count = ctx.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert count == 2

    # Original event still readable verbatim.
    from afair.substrate import read_event_by_hash

    still_there = read_event_by_hash(ctx.db, original.content_hash)
    assert still_there is not None
    assert still_there.payload["text"] == "Sajinth is CEO"


def test_read_invalidation_returns_latest_when_multiple(ctx: ServerContext) -> None:
    """If two invalidations target the same hash, the most recent wins."""
    import time

    target = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "fact"},
    )
    write_invalidation(
        ctx.db, target_hash=target.content_hash, reason="first attempt", origin="agent"
    )
    time.sleep(0.005)  # ensure created_at ordering is unambiguous
    second = write_invalidation(
        ctx.db,
        target_hash=target.content_hash,
        reason="second, definitive",
        origin="agent",
    )

    info = read_invalidation(ctx.db, target.content_hash)
    assert info is not None
    assert info.by_event_id == second.id
    assert info.reason == "second, definitive"


def test_read_invalidation_returns_none_when_not_invalidated(ctx: ServerContext) -> None:
    e = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "still true"},
    )
    assert read_invalidation(ctx.db, e.content_hash) is None


def test_batch_lookup_returns_only_invalidated_hashes(ctx: ServerContext) -> None:
    """``read_invalidations_batch`` returns a dict keyed by content_hash;
    hashes with no invalidation are absent."""
    e1 = write_event(
        ctx.db, origin="u", kind="remember", payload={"content_type": "text", "text": "a"}
    )
    e2 = write_event(
        ctx.db, origin="u", kind="remember", payload={"content_type": "text", "text": "b"}
    )
    e3 = write_event(
        ctx.db, origin="u", kind="remember", payload={"content_type": "text", "text": "c"}
    )
    write_invalidation(ctx.db, target_hash=e1.content_hash, reason="wrong", origin="a")
    write_invalidation(ctx.db, target_hash=e3.content_hash, reason="outdated", origin="a")

    out = read_invalidations_batch(ctx.db, [e1.content_hash, e2.content_hash, e3.content_hash])
    assert set(out.keys()) == {e1.content_hash, e3.content_hash}
    assert e2.content_hash not in out
    assert isinstance(out[e1.content_hash], InvalidationInfo)


def test_batch_lookup_empty_input_returns_empty(ctx: ServerContext) -> None:
    assert read_invalidations_batch(ctx.db, []) == {}


# ── handler layer (collapsed: invalidate is now a remember kwarg) ─────────


def test_remember_with_invalidates_writes_event_and_supersession(
    ctx: ServerContext,
) -> None:
    """remember(content, invalidates=[hash]) writes the new fact AND the
    invalidation event for each target. The result's invalidated list
    reflects what was successfully marked superseded."""
    r = handlers.remember(content=TextContent(type="text", text="we'll launch in March"))
    result = handlers.remember(
        content=TextContent(type="text", text="actually June"),
        invalidates=[r.content_hash],
    )
    assert result.ok is True
    assert r.content_hash in result.invalidated


def test_remember_invalidates_multiple_targets_in_one_call(
    ctx: ServerContext,
) -> None:
    """Multiple targets get individual invalidation events."""
    a = handlers.remember(content=TextContent(type="text", text="fact a"))
    b = handlers.remember(content=TextContent(type="text", text="fact b"))
    result = handlers.remember(
        content=TextContent(type="text", text="superseder"),
        invalidates=[a.content_hash, b.content_hash],
    )
    assert set(result.invalidated) == {a.content_hash, b.content_hash}


def test_remember_with_invalidates_unknown_target_raises(ctx: ServerContext) -> None:
    with pytest.raises(InvalidateTargetError, match="not found"):
        handlers.remember(
            content=TextContent(type="text", text="x"),
            invalidates=["sha256:" + "0" * 64],
        )


def test_remember_cannot_invalidate_an_invalidation_event(ctx: ServerContext) -> None:
    """Nested invalidations are not supported. The first invalidate goes
    through; trying to invalidate THE invalidation event must fail."""
    r = handlers.remember(content=TextContent(type="text", text="fact"))
    handlers.remember(
        content=TextContent(type="text", text="wrong"),
        invalidates=[r.content_hash],
    )
    # Find the invalidation event by querying for kind=invalidate.
    inv_row = ctx.db.execute(
        "SELECT content_hash FROM events WHERE kind = 'invalidate' LIMIT 1"
    ).fetchone()
    assert inv_row is not None
    with pytest.raises(InvalidateTargetError, match="invalidation event"):
        handlers.remember(
            content=TextContent(type="text", text="meta"),
            invalidates=[inv_row["content_hash"]],
        )


# ── recall integration ────────────────────────────────────────────────────


def test_recall_surfaces_invalidation_field_when_present(ctx: ServerContext) -> None:
    """Invalidated facts still appear in recall — they're not filtered —
    but each hit carries the invalidation summary so the AI can choose."""
    r = handlers.remember(content=TextContent(type="text", text="quarterly revenue is 100k"))
    handlers.remember(
        content=TextContent(type="text", text="restated to 92k"),
        invalidates=[r.content_hash],
    )

    hits = handlers.recall(query="quarterly revenue", depth="shallow").hits
    target_hit = next((h for h in hits if h.content_hash == r.content_hash), None)
    assert target_hit is not None
    assert target_hit.invalidation is not None
    assert "92k" in target_hit.invalidation.reason


def test_recall_leaves_invalidation_null_for_current_facts(ctx: ServerContext) -> None:
    handlers.remember(content=TextContent(type="text", text="current fact"))
    hits = handlers.recall(query="current", depth="shallow").hits
    assert len(hits) == 1
    assert hits[0].invalidation is None


def test_recall_by_id_surfaces_invalidation(ctx: ServerContext) -> None:
    r = handlers.remember(content=TextContent(type="text", text="old plan"))
    handlers.remember(
        content=TextContent(type="text", text="superseded by new plan"),
        invalidates=[r.content_hash],
    )
    result = handlers.recall(by_id=r.event_id)
    assert len(result.hits) == 1
    inv = result.hits[0].invalidation
    assert inv is not None
    assert "superseded" in inv.reason


def test_invalidation_event_itself_recallable_via_fts(ctx: ServerContext) -> None:
    """The invalidation event is itself a substrate event — its ``reason``
    is FTS-indexed via derive_searchable_text. Useful for 'what facts have
    we marked outdated?' queries."""
    r = handlers.remember(content=TextContent(type="text", text="original fact"))
    handlers.remember(
        content=TextContent(type="text", text="zombiequickfoxtrot unique marker"),
        invalidates=[r.content_hash],
    )
    hits = handlers.recall(query="zombiequickfoxtrot", depth="shallow").hits
    assert any(h.kind == INVALIDATE_KIND for h in hits)
