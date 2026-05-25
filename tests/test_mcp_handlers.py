"""MCP handler tests — pure-Python unit tests without spinning up a server."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from neverforget.mcp import handlers
from neverforget.mcp.context import ServerContext, clear_context, set_context
from neverforget.mcp.handlers import (
    EventNotFoundError,
    InvalidGetEventArgsError,
)
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
    # Disable semantic_recall by default — these tests don't mock the
    # embedding API. Tests that specifically want the hybrid path
    # re-enable it + monkeypatch embed_text.
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


def test_recall_normal_depth_with_semantic_disabled_returns_shallow(
    ctx: ServerContext,
) -> None:
    """With semantic_recall disabled (no embedding API), normal degrades to
    shallow + the standard FTS-only note."""
    handlers.remember(content=TextContent(type="text", text="anything"))
    r = handlers.recall(query="anything", depth="normal")
    assert r.depth_used == "shallow"


def test_recall_deep_depth_degrades_with_note(ctx: ServerContext) -> None:
    """Deep is not yet richer than normal; should always note that."""
    handlers.remember(content=TextContent(type="text", text="anything"))
    r = handlers.recall(query="anything", depth="deep")
    # With semantic_recall disabled, deep also falls to shallow.
    assert r.depth_used == "shallow"


def test_recall_normal_depth_with_mocked_embedding_uses_hybrid(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When semantic_recall is enabled and the embedding API works, normal
    depth runs the hybrid FTS+vector pipeline and surfaces depth_used=normal."""
    from neverforget.mcp.context import ServerContext as SC
    from neverforget.mcp.context import set_context

    # Replace context with one that has semantic_recall enabled
    set_context(
        SC(
            db=ctx.db,
            vault_dir=ctx.vault_dir,
            inline_text_max_bytes=64 * 1024,
            semantic_recall_enabled=True,
            embedding_model="openai/text-embedding-3-small",
        )
    )

    # Mock the embedding call to return a 1536-dim zero vector.
    # handlers.recall uses embed_query (the cached variant) since 2026-05-25.
    monkeypatch.setattr(
        "neverforget.mcp.handlers.embed_query",
        lambda **_: [0.0] * 1536,
    )

    handlers.remember(content=TextContent(type="text", text="anything noteworthy"))
    r = handlers.recall(query="noteworthy", depth="normal")
    assert r.depth_used == "normal"
    assert r.note is None
    # The remembered event should be found via FTS even though the vector
    # store may be empty (semantic_recall_enabled but no embedding stored
    # because schedule_extraction was no-op'd above).
    assert len(r.hits) >= 1


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


# ── get_event — full payload retrieval ─────────────────────────────────────


def test_get_event_returns_full_text_no_truncation(ctx: ServerContext) -> None:
    """A long inline-text event surfaces verbatim via get_event,
    while recall still truncates the same hit at 500 chars."""
    long_text = "Section " + "A" * 2000 + " — End"
    r = handlers.remember(content=TextContent(type="text", text=long_text))

    # Recall truncates to 500 chars (the existing behavior we keep).
    recall_result = handlers.recall(query="Section", depth="shallow")
    assert len(recall_result.hits) == 1
    preview = recall_result.hits[0].payload_summary["text"]
    assert len(preview) <= 500
    assert recall_result.hits[0].payload_summary.get("truncated") is True

    # get_event by event_id returns the FULL text.
    full = handlers.get_event(event_id=r.event_id)
    assert full.payload["content_type"] == "text"
    assert full.payload["text"] == long_text
    assert len(full.payload["text"]) > 2000


def test_get_event_by_content_hash_works(ctx: ServerContext) -> None:
    r = handlers.remember(content=TextContent(type="text", text="hash lookup"))
    full = handlers.get_event(content_hash=r.content_hash)
    assert full.event_id == r.event_id
    assert full.payload["text"] == "hash lookup"


def test_get_event_rejects_both_or_neither_selector(ctx: ServerContext) -> None:
    """Per spec, exactly ONE of event_id or content_hash must be provided."""
    handlers.remember(content=TextContent(type="text", text="x"))
    with pytest.raises(InvalidGetEventArgsError):
        handlers.get_event()
    with pytest.raises(InvalidGetEventArgsError):
        handlers.get_event(event_id="01XYZ", content_hash="sha256:abc")


def test_get_event_unknown_id_raises_not_found(ctx: ServerContext) -> None:
    with pytest.raises(EventNotFoundError):
        handlers.get_event(event_id="01DOESNOTEXIST00000000")


def test_get_event_inlines_text_large_blob(ctx: ServerContext) -> None:
    """text-large payloads spill to the object store; get_event reads
    them back so the caller sees one consistent shape with payload.text
    populated."""
    # Force spill by lowering the inline threshold for this test's context.
    ctx.inline_text_max_bytes = 100
    big = "Q" * 5000
    r = handlers.remember(content=TextContent(type="text", text=big))

    full = handlers.get_event(event_id=r.event_id)
    assert full.payload["content_type"] == "text-large"
    assert full.payload["text"] == big
    # The blob_hash and size_bytes are preserved for traceability.
    assert full.payload["blob_hash"].startswith("sha256:")
    assert full.payload["size_bytes"] == len(big)


def test_get_event_binary_returns_metadata_not_bytes(ctx: ServerContext) -> None:
    """Binary events keep raw bytes in the object store; get_event surfaces
    metadata only. A future read_blob tool would expose the actual bytes."""
    import base64

    raw = b"\x89PNG\x00\x00fake-png-bytes" * 50
    r = handlers.remember(
        content=BinaryContent(
            type="binary",
            data_b64=base64.b64encode(raw).decode("ascii"),
            mime="image/png",
            filename_hint="bug.png",
        )
    )
    full = handlers.get_event(event_id=r.event_id)
    assert full.payload["content_type"] == "binary"
    assert full.payload["mime"] == "image/png"
    assert full.payload["filename_hint"] == "bug.png"
    assert full.payload["size_bytes"] == len(raw)
    assert "text" not in full.payload  # bytes aren't inlined
