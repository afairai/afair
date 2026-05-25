"""MCP handler tests — pure-Python unit tests without spinning up a server."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from neverforget.mcp import handlers
from neverforget.mcp.context import ServerContext, clear_context, set_context
from neverforget.mcp.schemas import (
    MAX_REMEMBER_BYTES,
    BinaryContent,
    ObserveEvent,
    TextContent,
)
from neverforget.substrate import open_db

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _disable_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Handler tests don't exercise the extractor — disable it globally.

    Real LLM calls would (a) make tests flaky/slow and (b) require live keys
    in CI. The extractor itself is tested separately in test_extractor.py.
    """
    monkeypatch.setattr(
        "neverforget.mcp.handlers.schedule_extraction",
        lambda _event_id: None,
    )


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[ServerContext]:
    db = open_db(tmp_path)
    sc = ServerContext(db=db, vault_dir=tmp_path, inline_text_max_bytes=64 * 1024)
    set_context(sc)
    try:
        yield sc
    finally:
        db.close()
        clear_context()


# ── remember: text ──────────────────────────────────────────────────────────


def test_remember_text_inline(ctx: ServerContext) -> None:
    result = handlers.remember(
        content=TextContent(type="text", text="hello world"),
        context="test",
    )
    assert result.ok is True
    assert result.deduplicated is False
    assert result.content_hash.startswith("sha256:")
    assert result.event_id


def test_remember_text_dedup(ctx: ServerContext) -> None:
    a = handlers.remember(
        content=TextContent(type="text", text="same content"),
        context="ctx",
    )
    b = handlers.remember(
        content=TextContent(type="text", text="same content"),
        context="ctx",
    )
    assert a.event_id == b.event_id
    assert b.deduplicated is True
    count = ctx.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert count == 1


def test_remember_text_too_large(ctx: ServerContext) -> None:
    too_big = "x" * (MAX_REMEMBER_BYTES + 1)
    with pytest.raises(handlers.ContentTooLargeError):
        handlers.remember(content=TextContent(type="text", text=too_big))


def test_remember_text_large_spills_to_object_store(ctx: ServerContext) -> None:
    """Text above the inline threshold but under the 10MB cap spills to disk."""
    payload = "x" * 200_000  # 200 KB > 64 KB inline threshold
    result = handlers.remember(content=TextContent(type="text", text=payload))
    assert result.ok is True
    # The event row's payload should reference an object-store blob.
    row = ctx.db.execute("SELECT payload FROM events WHERE id = ?", (result.event_id,)).fetchone()
    import json

    p = json.loads(row["payload"])
    assert p["content_type"] == "text-large"
    assert p["blob_hash"].startswith("sha256:")


# ── remember: binary ────────────────────────────────────────────────────────


def test_remember_binary_basic(ctx: ServerContext) -> None:
    raw = b"\x89PNG\r\n\x1a\nfake-png-bytes"
    result = handlers.remember(
        content=BinaryContent(
            type="binary",
            data_b64=base64.b64encode(raw).decode("ascii"),
            mime="image/png",
            filename_hint="screenshot.png",
        ),
        context="bug hunt",
    )
    assert result.ok is True
    import json

    row = ctx.db.execute("SELECT payload FROM events WHERE id = ?", (result.event_id,)).fetchone()
    p = json.loads(row["payload"])
    assert p["content_type"] == "binary"
    assert p["mime"] == "image/png"
    assert p["filename_hint"] == "screenshot.png"
    assert p["blob_hash"].startswith("sha256:")
    # Blob is reachable from disk via the substrate's object store.
    from neverforget.substrate import read_object

    assert read_object(ctx.vault_dir, p["blob_hash"]) == raw


def test_remember_binary_invalid_base64(ctx: ServerContext) -> None:
    with pytest.raises(handlers.InvalidBase64Error):
        handlers.remember(
            content=BinaryContent(
                type="binary",
                data_b64="this is not !!! base64",
                mime="application/octet-stream",
            )
        )


def test_remember_binary_too_large(ctx: ServerContext) -> None:
    raw = b"\x00" * (MAX_REMEMBER_BYTES + 1)
    with pytest.raises(handlers.ContentTooLargeError):
        handlers.remember(
            content=BinaryContent(
                type="binary",
                data_b64=base64.b64encode(raw).decode("ascii"),
                mime="application/octet-stream",
            )
        )


def test_remember_binary_requires_mime() -> None:
    """Pydantic validation: mime must be non-empty in BinaryContent."""
    with pytest.raises(ValidationError):
        BinaryContent(type="binary", data_b64="aGk=", mime="")


# ── recall ──────────────────────────────────────────────────────────────────


def test_recall_shallow_finds_match(ctx: ServerContext) -> None:
    handlers.remember(
        content=TextContent(
            type="text",
            text="Sajinth proposed a new roadmap focused on memory",
        ),
        context="email",
    )
    handlers.remember(
        content=TextContent(type="text", text="unrelated lunch plans"),
    )

    r = handlers.recall(query="Sajinth")
    assert r.depth_used == "shallow"
    assert r.note is None
    assert len(r.hits) == 1
    assert "text" in r.hits[0].payload_summary
    assert "Sajinth" in r.hits[0].payload_summary["text"]


def test_recall_normal_depth_degrades_with_note(ctx: ServerContext) -> None:
    handlers.remember(content=TextContent(type="text", text="anything"))
    r = handlers.recall(query="anything", depth="normal")
    assert r.depth_used == "shallow"
    assert r.note is not None
    assert "normal" in r.note


def test_recall_deep_depth_degrades_with_note(ctx: ServerContext) -> None:
    handlers.remember(content=TextContent(type="text", text="anything"))
    r = handlers.recall(query="anything", depth="deep")
    assert r.depth_used == "shallow"
    assert r.note is not None
    assert "deep" in r.note


def test_recall_truncates_long_text_in_summary(ctx: ServerContext) -> None:
    # Long real text with a recognizable token so FTS5 actually matches.
    long_text = "important " + "filler word " * 1000
    handlers.remember(content=TextContent(type="text", text=long_text))
    r = handlers.recall(query="important")
    assert len(r.hits) == 1
    summary = r.hits[0].payload_summary
    assert summary["truncated"] is True
    assert len(summary["text"]) <= 500


def test_recall_empty_vault_returns_no_hits(ctx: ServerContext) -> None:
    r = handlers.recall(query="anything")
    assert r.hits == []
    assert r.depth_used == "shallow"


# ── list_context ────────────────────────────────────────────────────────────


def test_list_context_empty(ctx: ServerContext) -> None:
    r = handlers.list_context()
    assert r.summary.total_events == 0
    assert r.summary.by_kind == {}
    assert r.summary.by_origin == {}
    assert r.summary.recent == []


def test_list_context_counts_by_kind_and_origin(ctx: ServerContext) -> None:
    handlers.remember(content=TextContent(type="text", text="a"))
    handlers.remember(content=TextContent(type="text", text="b"))
    handlers.observe(event=ObserveEvent(action="edit_file", subject="foo.py"))

    r = handlers.list_context()
    assert r.summary.total_events == 3
    assert r.summary.by_kind == {"remember": 2, "observe": 1}
    assert r.summary.by_origin == {"agent": 3}


def test_list_context_about_filters(ctx: ServerContext) -> None:
    handlers.remember(
        content=TextContent(type="text", text="roadmap for Q3"),
        context="planning",
    )
    handlers.remember(content=TextContent(type="text", text="lunch ideas"))

    r = handlers.list_context(about="roadmap")
    assert len(r.summary.recent) == 1
    assert "roadmap" in r.summary.recent[0].payload_summary["text"]


# ── observe ─────────────────────────────────────────────────────────────────


def test_observe_basic(ctx: ServerContext) -> None:
    r = handlers.observe(
        event=ObserveEvent(
            action="edit_file",
            subject="events.py",
            result="added inline-vs-spill logic",
        )
    )
    assert r.ok is True
    assert r.content_hash.startswith("sha256:")


def test_observe_action_required() -> None:
    with pytest.raises(ValidationError):
        ObserveEvent(action="")


def test_observe_action_whitespace_only() -> None:
    with pytest.raises(ValidationError):
        ObserveEvent(action="   ")


def test_observe_preserves_extra_fields(ctx: ServerContext) -> None:
    """Arbitrary extra fields in event are preserved verbatim."""
    event = ObserveEvent.model_validate(
        {
            "action": "deployed",
            "subject": "neverforget-prod",
            "result": "v0.1.3",
            "duration_s": 47,
            "branch": "main",
            "commit_sha": "abc123",
        }
    )
    r = handlers.observe(event=event)

    import json

    row = ctx.db.execute("SELECT payload FROM events WHERE id = ?", (r.event_id,)).fetchone()
    p = json.loads(row["payload"])
    assert p["action"] == "deployed"
    assert p["duration_s"] == 47
    assert p["branch"] == "main"
    assert p["commit_sha"] == "abc123"
    assert p["content_type"] == "event"


def test_observe_then_recall_finds_it(ctx: ServerContext) -> None:
    """Observe events are indexed via action/subject/result — recall finds them."""
    handlers.observe(
        event=ObserveEvent(
            action="edit_file",
            subject="events.py",
            result="added inline spill logic",
        )
    )
    # Single-word query — avoids FTS5 special-char interpretation of hyphens.
    r = handlers.recall(query="inline")
    assert len(r.hits) == 1
    assert r.hits[0].payload_summary["action"] == "edit_file"
    assert r.hits[0].payload_summary["subject"] == "events.py"
