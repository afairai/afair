"""Event-provenance sidecar tests (ADR-0006, W1).

Covers the substrate layer (append-only triggers, record/read roundtrip,
INSERT OR IGNORE dedup, per-client counts), the write-path stamping in the
remember/observe handlers (fail-soft, outside the was_inserted guard), and the
additive serving (RecallHit.client + ContextSummary.by_client, verbosity-gated).
Middleware stamping + the JWT claim roundtrip live in test_auth.py; the export
ride-along lives in test_export_roundtrip.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from afair.mcp import handlers
from afair.mcp.context import ServerContext, clear_context, set_context
from afair.mcp.schemas import ObserveEvent, TextContent
from afair.substrate import (
    count_events_by_client,
    open_db,
    read_event_provenance_batch,
    record_event_provenance,
    write_event_with_status,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _no_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("afair.mcp.handlers.schedule_extraction", lambda _event_id: None)


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[ServerContext]:
    db = open_db(tmp_path)
    sc = ServerContext(
        db=db,
        vault_dir=tmp_path,
        inline_text_max_bytes=64 * 1024,
        semantic_recall_enabled=False,
    )
    set_context(sc)
    try:
        yield sc
    finally:
        db.close()
        clear_context()


def _write_event(ctx: ServerContext, text: str) -> str:
    event, _ = write_event_with_status(
        ctx.db, origin="agent", kind="remember", payload={"content_type": "text", "text": text}
    )
    return event.id


# ── substrate: append-only triggers ─────────────────────────────────────────


def test_event_provenance_is_append_only(ctx: ServerContext) -> None:
    event_id = _write_event(ctx, "hello")
    record_event_provenance(
        ctx.db, event_id=event_id, client="claude-code", auth_kind="oauth", verb="remember"
    )
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        ctx.db.execute("UPDATE event_provenance SET client = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        ctx.db.execute("DELETE FROM event_provenance")


# ── substrate: record / read / dedup ────────────────────────────────────────


def test_record_and_read_roundtrip(ctx: ServerContext) -> None:
    event_id = _write_event(ctx, "roundtrip")
    record_event_provenance(
        ctx.db, event_id=event_id, client="cursor", auth_kind="api-token", verb="remember"
    )
    rows = read_event_provenance_batch(ctx.db, [event_id])
    assert list(rows.keys()) == [event_id]
    (row,) = rows[event_id]
    assert row.client == "cursor"
    assert row.auth_kind == "api-token"
    assert row.verb == "remember"


def test_same_client_restamp_is_a_noop(ctx: ServerContext) -> None:
    event_id = _write_event(ctx, "dedup")
    for _ in range(3):
        record_event_provenance(
            ctx.db, event_id=event_id, client="claude-code", auth_kind="oauth", verb="remember"
        )
    rows = read_event_provenance_batch(ctx.db, [event_id])[event_id]
    assert len(rows) == 1  # INSERT OR IGNORE on UNIQUE(event_id, client)


def test_second_client_appends_an_honest_row(ctx: ServerContext) -> None:
    event_id = _write_event(ctx, "shared")
    record_event_provenance(
        ctx.db, event_id=event_id, client="claude-code", auth_kind="oauth", verb="remember"
    )
    record_event_provenance(
        ctx.db, event_id=event_id, client="cursor", auth_kind="api-token", verb="remember"
    )
    rows = read_event_provenance_batch(ctx.db, [event_id])[event_id]
    assert len(rows) == 2
    # Ordered by stamped_at ASC → author (first writer) first.
    assert [r.client for r in rows] == ["claude-code", "cursor"]


def test_read_batch_absent_for_unprovenanced_event(ctx: ServerContext) -> None:
    event_id = _write_event(ctx, "no-prov")
    assert read_event_provenance_batch(ctx.db, [event_id]) == {}
    assert read_event_provenance_batch(ctx.db, []) == {}


def test_count_events_by_client_distinct(ctx: ServerContext) -> None:
    e1 = _write_event(ctx, "a")
    e2 = _write_event(ctx, "b")
    # claude-code wrote both events; cursor co-wrote e1.
    for eid in (e1, e2):
        record_event_provenance(
            ctx.db, event_id=eid, client="claude-code", auth_kind="oauth", verb="remember"
        )
    record_event_provenance(
        ctx.db, event_id=e1, client="cursor", auth_kind="api-token", verb="remember"
    )
    assert count_events_by_client(ctx.db) == {"claude-code": 2, "cursor": 1}


# ── handler write-path stamping ─────────────────────────────────────────────


def test_remember_stamps_provenance(ctx: ServerContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers, "current_client", lambda: ("claude-code", "oauth"))
    result = handlers.remember(content=TextContent(type="text", text="stamped fact"))
    rows = read_event_provenance_batch(ctx.db, [result.event_id])[result.event_id]
    assert (rows[0].client, rows[0].auth_kind, rows[0].verb) == ("claude-code", "oauth", "remember")


def test_observe_stamps_provenance(ctx: ServerContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers, "current_client", lambda: ("cursor", "api-token"))
    result = handlers.observe(event=ObserveEvent(action="edited", subject="events.py"))
    rows = read_event_provenance_batch(ctx.db, [result.event_id])[result.event_id]
    assert (rows[0].client, rows[0].verb) == ("cursor", "observe")


def test_dedup_second_client_still_stamps(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dedup'd write (was_inserted=False) from a different client must still
    record that client — the stamp sits OUTSIDE the was_inserted guard."""
    monkeypatch.setattr(handlers, "current_client", lambda: ("claude-code", "oauth"))
    first = handlers.remember(content=TextContent(type="text", text="same text"))
    monkeypatch.setattr(handlers, "current_client", lambda: ("cursor", "api-token"))
    second = handlers.remember(content=TextContent(type="text", text="same text"))
    assert second.deduplicated is True
    assert second.event_id == first.event_id  # same content hash → same row
    rows = read_event_provenance_batch(ctx.db, [first.event_id])[first.event_id]
    assert {r.client for r in rows} == {"claude-code", "cursor"}


def test_no_http_context_writes_no_row(ctx: ServerContext) -> None:
    """current_client() returns None outside an HTTP request (the unit-test /
    cold-path path) — the write succeeds and no provenance row is written."""
    result = handlers.remember(content=TextContent(type="text", text="direct call"))
    assert result.ok is True
    assert read_event_provenance_batch(ctx.db, [result.event_id]) == {}


def test_stamp_failure_is_fail_soft(ctx: ServerContext, monkeypatch: pytest.MonkeyPatch) -> None:
    """A provenance-stamp failure must never fail the underlying remember."""
    monkeypatch.setattr(handlers, "current_client", lambda: ("claude-code", "oauth"))

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("provenance table gone")

    monkeypatch.setattr(handlers, "record_event_provenance", _boom)
    result = handlers.remember(content=TextContent(type="text", text="still ok"))
    assert result.ok is True
    assert read_event_provenance_batch(ctx.db, [result.event_id]) == {}


# ── serving: RecallHit.client (verbosity-gated) + ContextSummary.by_client ───


def test_client_served_at_standard_and_full_omitted_compact(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handlers, "current_client", lambda: ("claude-code", "oauth"))
    handlers.remember(content=TextContent(type="text", text="provenancetoken alpha"))

    for verbosity in ("standard", "full"):
        res = handlers.recall(query="provenancetoken", depth="shallow", verbosity=verbosity)
        assert res.hits, verbosity
        assert res.hits[0].client == "claude-code", verbosity

    compact = handlers.recall(query="provenancetoken", depth="shallow", verbosity="compact")
    assert compact.hits
    # compact drops the field entirely from the wire (exclude_none).
    assert compact.hits[0].client is None
    assert "client" not in compact.hits[0].model_dump(exclude_none=True)


def test_client_served_by_id(ctx: ServerContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers, "current_client", lambda: ("windsurf", "oauth"))
    written = handlers.remember(content=TextContent(type="text", text="lookup me"))
    res = handlers.recall(by_id=written.event_id)
    assert res.hits[0].client == "windsurf"


def test_legacy_event_has_null_client(ctx: ServerContext) -> None:
    """An event with no provenance row (pre-provenance / non-HTTP) serves
    client=None, never an error."""
    written = handlers.remember(content=TextContent(type="text", text="legacy event"))
    res = handlers.recall(by_id=written.event_id)
    assert res.hits[0].client is None


def test_stats_by_client_distinct_by_origin_unchanged(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handlers, "current_client", lambda: ("claude-code", "oauth"))
    handlers.remember(content=TextContent(type="text", text="one"))
    monkeypatch.setattr(handlers, "current_client", lambda: ("cursor", "api-token"))
    handlers.observe(event=ObserveEvent(action="ran"))

    res = handlers.recall(stats=True)
    assert res.summary is not None
    assert res.summary.by_client == {"claude-code": 1, "cursor": 1}
    # by_origin is a different axis (both events carry origin "agent") — untouched.
    assert res.summary.by_origin == {"agent": 2}
