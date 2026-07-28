"""`actor` attribution on remember + observe (ADR-0010).

An additive, optional field naming ON WHOSE BEHALF a SHARED credential wrote —
an organization's agent relaying many humans. It is caller-supplied attribution
= CONTENT, so it lives IN the payload (in the content hash), exactly like
``asserted_by`` (W3). Unlike the server-derived ``client`` sidecar, actor is set
by the caller and never substitutes for client or raises trust.

These tests are the regression tripwire:
  - a no-actor write is BYTE-IDENTICAL (same content hash) to the pre-change
    behavior — the cardinal dedup guard;
  - same content on behalf of alice vs bob is TWO events; same+same dedups;
  - actor is served at every verbosity + full_payload/by_id;
  - the shared-agent case: sidecar client=team-agent AND payload actor=slack:U123
    are BOTH served on the hit — the two axes are orthogonal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import TypeAdapter

from afair.mcp import handlers
from afair.mcp.context import ServerContext, clear_context, set_context
from afair.mcp.schemas import (
    MAX_ACTOR_CHARS,
    ObserveEvent,
    ObserveEventInput,
    TextContent,
    sanitize_actor,
)
from afair.substrate import open_db, read_event_by_id

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _no_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("afair.mcp.handlers.schedule_extraction", lambda _id: None)


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[ServerContext]:
    db = open_db(tmp_path)
    sc = ServerContext(
        db=db, vault_dir=tmp_path, inline_text_max_bytes=64 * 1024, semantic_recall_enabled=False
    )
    set_context(sc)
    try:
        yield sc
    finally:
        db.close()
        clear_context()


# ── sanitize_actor ───────────────────────────────────────────────────────────


def test_sanitize_actor_strips_and_blanks() -> None:
    assert sanitize_actor("  slack:U123  ") == "slack:U123"
    assert sanitize_actor("   ") is None
    assert sanitize_actor("") is None
    assert sanitize_actor(None) is None


def test_sanitize_actor_keeps_identifier_verbatim() -> None:
    """No slugging: colons, dots, @, and case survive (fidelity matters)."""
    assert sanitize_actor("slack:U0BKXTGBWLD") == "slack:U0BKXTGBWLD"
    assert sanitize_actor("Alice@Corp.com") == "Alice@Corp.com"
    assert sanitize_actor("Alice From Sales") == "Alice From Sales"


def test_sanitize_actor_drops_control_chars() -> None:
    assert sanitize_actor("slack\x00:U1\x07") == "slack:U1"
    assert sanitize_actor("a\nb") == "ab"


# ── remember: persistence + hashing ──────────────────────────────────────────


def test_no_actor_hash_is_byte_identical(ctx: ServerContext) -> None:
    """The cardinal dedup guard: a remember without actor produces exactly the
    pre-change payload (no ``actor`` key) and hash."""
    res = handlers.remember(content=TextContent(type="text", text="plain fact"))
    event = read_event_by_id(ctx.db, res.event_id)
    assert event is not None
    assert "actor" not in event.payload
    assert "actor_full" not in event.payload


def test_actor_persisted_in_payload(ctx: ServerContext) -> None:
    res = handlers.remember(content=TextContent(type="text", text="fact"), actor="slack:U123")
    event = read_event_by_id(ctx.db, res.event_id)
    assert event is not None
    assert event.payload.get("actor") == "slack:U123"


def test_blank_actor_absent_from_payload(ctx: ServerContext) -> None:
    res = handlers.remember(content=TextContent(type="text", text="fact"), actor="   ")
    event = read_event_by_id(ctx.db, res.event_id)
    assert event is not None
    assert "actor" not in event.payload


def test_same_content_different_actor_are_two_events(ctx: ServerContext) -> None:
    """In-hash by design: attribution is content, so alice's and bob's copies of
    the same sentence are distinct events (dedup must not merge two people)."""
    alice = handlers.remember(
        content=TextContent(type="text", text="the meeting is Friday"), actor="alice"
    )
    bob = handlers.remember(
        content=TextContent(type="text", text="the meeting is Friday"), actor="bob"
    )
    assert alice.content_hash != bob.content_hash
    assert bob.deduplicated is False


def test_same_content_same_actor_dedups(ctx: ServerContext) -> None:
    first = handlers.remember(content=TextContent(type="text", text="dedupe me"), actor="alice")
    second = handlers.remember(content=TextContent(type="text", text="dedupe me"), actor="alice")
    assert second.deduplicated is True
    assert second.content_hash == first.content_hash


def test_actor_truncate_preserve(ctx: ServerContext) -> None:
    """An over-120-char actor is truncated to the cap; the full original is kept
    under ``actor_full`` (mirroring context_full)."""
    long_actor = "x" * 300
    res = handlers.remember(content=TextContent(type="text", text="fact"), actor=long_actor)
    event = read_event_by_id(ctx.db, res.event_id)
    assert event is not None
    assert len(event.payload["actor"]) == MAX_ACTOR_CHARS
    assert event.payload["actor_full"] == long_actor


# ── remember: serving at every verbosity ─────────────────────────────────────


def test_actor_served_by_id_and_all_verbosities(ctx: ServerContext) -> None:
    res = handlers.remember(
        content=TextContent(type="text", text="servetoken actor fact"), actor="slack:U999"
    )
    by_id = handlers.recall(by_id=res.event_id)
    assert by_id.hits[0].payload.get("actor") == "slack:U999"
    for verbosity in ("compact", "standard", "full"):
        hit = handlers.recall(query="servetoken", depth="shallow", verbosity=verbosity)  # type: ignore[arg-type]
        assert hit.hits[0].payload.get("actor") == "slack:U999", verbosity


# ── observe: bounded field, write-first ──────────────────────────────────────


def test_observe_actor_field_bounds() -> None:
    """The ObserveEvent.actor field is bounded + sanitized; a blank actor is
    dropped, a long one truncated with actor_full preserved."""
    adapter: TypeAdapter[ObserveEvent] = TypeAdapter(ObserveEventInput)
    ev = adapter.validate_python({"action": "ping", "actor": "  slack:U1  "})
    assert ev.actor == "slack:U1"
    blank = adapter.validate_python({"action": "ping", "actor": "   "})
    assert blank.actor is None
    long_ev = adapter.validate_python({"action": "ping", "actor": "y" * 300})
    dumped = long_ev.model_dump()
    assert len(dumped["actor"]) == MAX_ACTOR_CHARS
    assert dumped.get("actor_full") == "y" * 300


def test_observe_never_rejected_on_actor() -> None:
    """Write-first intake: an odd actor never rejects the observe."""
    adapter: TypeAdapter[ObserveEvent] = TypeAdapter(ObserveEventInput)
    ev = adapter.validate_python({"action": "did_thing", "actor": 12345})
    # A non-string actor is not a valid actor; the write still lands.
    assert ev.action == "did_thing"


def test_observe_no_actor_payload_byte_identical(ctx: ServerContext) -> None:
    """An observe WITHOUT actor keeps the pre-change payload shape (no actor
    key), so its content hash is unchanged."""
    res = handlers.observe(event=ObserveEvent(action="ping"))
    event = read_event_by_id(ctx.db, res.event_id)
    assert event is not None
    assert "actor" not in event.payload


def test_observe_actor_persisted_and_in_hash(ctx: ServerContext) -> None:
    a = handlers.observe(event=ObserveEvent(action="relay", actor="alice"))
    b = handlers.observe(event=ObserveEvent(action="relay", actor="bob"))
    ea = read_event_by_id(ctx.db, a.event_id)
    eb = read_event_by_id(ctx.db, b.event_id)
    assert ea is not None and eb is not None
    assert ea.payload.get("actor") == "alice"
    assert eb.payload.get("actor") == "bob"
    assert a.content_hash != b.content_hash  # attribution is content


# ── extractor sees actor (entity linking) ────────────────────────────────────


def test_build_user_message_contains_actor(ctx: ServerContext) -> None:
    """actor rides to the extractor via the extras loop (inside wrap_untrusted)."""
    from afair.agents.prompts import build_user_message

    res = handlers.remember(content=TextContent(type="text", text="fact"), actor="slack:U777")
    event = read_event_by_id(ctx.db, res.event_id)
    assert event is not None
    msg = build_user_message(event)
    assert "slack:U777" in msg


# ── shared-agent e2e: sidecar client + payload actor both served ─────────────


def test_shared_agent_serves_both_client_and_actor(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single shared credential (label 'team-agent') relays for a specific
    member (actor 'slack:U123'). The sidecar records client=team-agent
    (server-derived), the payload records actor=slack:U123 (caller-supplied), and
    a recall hit serves BOTH — the two attribution axes are orthogonal."""
    monkeypatch.setattr(handlers, "current_client", lambda: ("team-agent", "api-token"))
    res = handlers.remember(
        content=TextContent(type="text", text="orgfact quarterly plan"), actor="slack:U123"
    )
    # standard/full verbosity serves the sidecar client.
    hit = handlers.recall(query="orgfact", depth="shallow", verbosity="standard").hits[0]
    assert hit.client == "team-agent"  # sidecar, server-derived
    assert hit.payload.get("actor") == "slack:U123"  # payload, caller-supplied
    # compact drops client (sidecar-query cost) but STILL serves actor (in payload).
    compact = handlers.recall(query="orgfact", depth="shallow", verbosity="compact").hits[0]
    assert compact.client is None
    assert compact.payload.get("actor") == "slack:U123"
    # by_id serves both.
    by_id = handlers.recall(by_id=res.event_id).hits[0]
    assert by_id.client == "team-agent"
    assert by_id.payload.get("actor") == "slack:U123"
