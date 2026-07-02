"""MCP handler tests — pure-Python unit tests without spinning up a server."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

import pytest
from pydantic import TypeAdapter, ValidationError

from afair.mcp import handlers
from afair.mcp.context import ServerContext, clear_context, set_context
from afair.mcp.handlers import InvalidRecallArgsError
from afair.mcp.schemas import (
    MAX_OBSERVE_ACTION_CHARS,
    MAX_REMEMBER_BYTES,
    BinaryContent,
    ObserveEvent,
    RememberContentInput,
    TextContent,
)
from afair.substrate import open_db

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
        "afair.mcp.handlers.schedule_extraction",
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
    from afair.substrate import read_object

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
    assert "text" in r.hits[0].payload
    assert "Sajinth" in r.hits[0].payload["text"]


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
    from afair.mcp.context import ServerContext as SC
    from afair.mcp.context import set_context

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
        "afair.mcp.handlers.embed_query",
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
    hit = r.hits[0]
    # The truncated flag lives on the hit itself in the new shape.
    assert hit.truncated is True
    assert len(hit.payload["text"]) <= 500


def test_recall_empty_vault_returns_no_hits(ctx: ServerContext) -> None:
    r = handlers.recall(query="anything")
    assert r.hits == []
    assert r.depth_used == "shallow"


# ── list_context ────────────────────────────────────────────────────────────


def test_recall_stats_empty_vault(ctx: ServerContext) -> None:
    """recall(stats=True) replaces the old list_context — same semantics."""
    r = handlers.recall(stats=True)
    assert r.summary is not None
    assert r.summary.total_events == 0
    assert r.summary.by_kind == {}
    assert r.summary.by_origin == {}
    assert r.hits == []  # no events yet


def test_recall_stats_counts_by_kind_and_origin(ctx: ServerContext) -> None:
    handlers.remember(content=TextContent(type="text", text="a"))
    handlers.remember(content=TextContent(type="text", text="b"))
    handlers.observe(event=ObserveEvent(action="edit_file", subject="foo.py"))

    r = handlers.recall(stats=True)
    assert r.summary is not None
    assert r.summary.total_events == 3
    assert r.summary.by_kind == {"remember": 2, "observe": 1}
    assert r.summary.by_origin == {"agent": 3}


def test_recall_with_query_and_stats(ctx: ServerContext) -> None:
    handlers.remember(
        content=TextContent(type="text", text="roadmap for Q3"),
        context="planning",
    )
    handlers.remember(content=TextContent(type="text", text="lunch ideas"))

    r = handlers.recall(query="roadmap", stats=True)
    # search returns the relevant hit, summary covers the whole vault
    assert len(r.hits) == 1
    assert "roadmap" in r.hits[0].payload["text"]
    assert r.summary is not None
    assert r.summary.total_events == 2


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


def test_observe_missing_action_defaults_not_rejected() -> None:
    # Write-first intake: a blank/missing action is defaulted, never rejected,
    # so the event (subject/result/extras) is still logged instead of dropped.
    assert ObserveEvent(action="").action == "observed"
    assert ObserveEvent(action="   ").action == "observed"
    e = ObserveEvent.model_validate({"subject": "file.py", "result": "edited"})
    assert e.action == "observed"
    assert e.subject == "file.py"


def test_observe_bare_string_becomes_action() -> None:
    assert ObserveEvent.model_validate("did a thing").action == "did a thing"


def test_observe_stringified_object_parsed_and_truncated() -> None:
    # T10: a JSON-serialized event string decodes to its fields (action/subject),
    # and an over-long action still truncates with the full value preserved —
    # the JSON-string decode composes with the AFAIR-H truncation.
    import json

    long_action = "x" * 250
    e = ObserveEvent.model_validate(json.dumps({"action": long_action, "subject": "s"}))
    assert e.subject == "s"
    assert len(e.action) == MAX_OBSERVE_ACTION_CHARS
    dumped = e.model_dump()
    assert dumped["action_full"] == long_action


def test_observe_stringified_non_dict_falls_back_to_action() -> None:
    # T10: a string that json-parses to a non-dict (a quoted JSON string) is not
    # an object, so the ORIGINAL string lands as the action verbatim — nothing
    # is lost (matches the remember non-dict fallback).
    import json

    raw = json.dumps("quoted json string")  # -> '"quoted json string"'
    e = ObserveEvent.model_validate(raw)
    assert e.action == raw


# ── remember: write-first content coercion (never lose a memory) ─────────────

_content = TypeAdapter(RememberContentInput)


def test_remember_content_stray_type_coerced_to_text() -> None:
    # The AFAIR-H shape: an agent put its type_hint value into content.type.
    # Old behaviour raised union_tag_invalid and the memory was lost; now it is
    # coerced to a text event instead.
    out = _content.validate_python({"text": "AUFLOESUNG der Sache", "type": "fact"})
    assert isinstance(out, TextContent)
    assert out.text == "AUFLOESUNG der Sache"


def test_remember_content_bare_string_becomes_text() -> None:
    out = _content.validate_python("just a note")
    assert isinstance(out, TextContent)
    assert out.text == "just a note"


def test_remember_content_unsalvageable_stored_as_raw_text() -> None:
    # A valid tag but missing required fields (type:binary, no data) can't be a
    # binary event; rather than reject, the raw payload is kept as text.
    out = _content.validate_python({"type": "binary"})
    assert isinstance(out, TextContent)
    assert "binary" in out.text  # the raw payload survived, serialized


def test_remember_valid_content_passes_through_unchanged() -> None:
    out = _content.validate_python({"type": "text", "text": "hi"})
    assert isinstance(out, TextContent)
    assert out.text == "hi"


def test_remember_content_stringified_object_parsed() -> None:
    # T9: a JSON-serialized object string decodes to the intended object,
    # not stored as literal text.
    import json

    out = _content.validate_python(json.dumps({"type": "text", "text": "hi there"}))
    assert isinstance(out, TextContent)
    assert out.text == "hi there"


def test_remember_content_stringified_non_dict_falls_back_to_text() -> None:
    # T9: a string that json-parses to a non-dict (list/number/bool) is NOT an
    # object, so it falls to the bare-string tolerance and is stored verbatim.
    out = _content.validate_python("[1, 2, 3]")
    assert isinstance(out, TextContent)
    assert out.text == "[1, 2, 3]"


def test_observe_preserves_extra_fields(ctx: ServerContext) -> None:
    """Arbitrary extra fields in event are preserved verbatim."""
    event = ObserveEvent.model_validate(
        {
            "action": "deployed",
            "subject": "afair-prod",
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


def test_observe_extras_cannot_override_reserved_payload_keys(ctx: ServerContext) -> None:
    """Caller-supplied extras must not spoof reserved payload keys.

    Regression: ObserveEvent allows arbitrary extras, and the payload was
    built as {"content_type": "event", **event_dict}, so a caller-supplied
    content_type (e.g. "text-large") plus a blob_hash of an EXISTING blob
    made the extractor rehydrate an unrelated blob as this event's body.
    """
    event = ObserveEvent.model_validate(
        {
            "action": "spoof_attempt",
            "subject": "handlers.py",
            "content_type": "text-large",
            "blob_hash": "sha256:" + "ab" * 32,
            "text": "smuggled body",
            "parts": [{"type": "text", "text": "smuggled part"}],
            "legit_extra": "kept",
        }
    )
    r = handlers.observe(event=event)

    import json

    row = ctx.db.execute("SELECT payload FROM events WHERE id = ?", (r.event_id,)).fetchone()
    p = json.loads(row["payload"])
    # Reserved keys win: content_type is pinned, modality-dispatch keys gone.
    assert p["content_type"] == "event"
    assert "blob_hash" not in p
    assert "text" not in p
    assert "parts" not in p
    # Legitimate fields and extras survive.
    assert p["action"] == "spoof_attempt"
    assert p["subject"] == "handlers.py"
    assert p["legit_extra"] == "kept"


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
    assert r.hits[0].payload["action"] == "edit_file"
    assert r.hits[0].payload["subject"] == "events.py"


# ── recall by_id / by_content_hash / full_payload (absorbs get_event) ─────


def test_recall_by_id_returns_full_text_no_truncation(ctx: ServerContext) -> None:
    """A long inline-text event surfaces verbatim via recall(by_id=...,
    full_payload=True), while default recall(query) still truncates."""
    long_text = "Section " + "A" * 2000 + " — End"
    r = handlers.remember(content=TextContent(type="text", text=long_text))

    # Default recall truncates the text payload.
    recall_result = handlers.recall(query="Section", depth="shallow")
    assert len(recall_result.hits) == 1
    preview = recall_result.hits[0].payload["text"]
    assert len(preview) <= 500
    assert recall_result.hits[0].truncated is True

    # by_id lookup returns the full event (full_payload implicit).
    full = handlers.recall(by_id=r.event_id)
    assert len(full.hits) == 1
    assert full.hits[0].payload["content_type"] == "text"
    assert full.hits[0].payload["text"] == long_text
    assert full.hits[0].truncated is False


def test_recall_by_content_hash_works(ctx: ServerContext) -> None:
    r = handlers.remember(content=TextContent(type="text", text="hash lookup"))
    full = handlers.recall(by_content_hash=r.content_hash)
    assert len(full.hits) == 1
    assert full.hits[0].event_id == r.event_id
    assert full.hits[0].payload["text"] == "hash lookup"


def test_recall_rejects_both_lookup_selectors(ctx: ServerContext) -> None:
    """recall accepts at most ONE of by_id, by_content_hash."""
    handlers.remember(content=TextContent(type="text", text="x"))
    with pytest.raises(InvalidRecallArgsError):
        handlers.recall(by_id="01XYZ", by_content_hash="sha256:abc")


def test_recall_unknown_id_returns_empty_with_note(ctx: ServerContext) -> None:
    """Unlike the old get_event (which raised), recall(by_id=...) returns
    an empty hits list with a note. Friendlier for AI clients that
    speculatively query."""
    r = handlers.recall(by_id="01DOESNOTEXIST00000000")
    assert r.hits == []
    assert r.note is not None and "no event found" in r.note


def test_recall_full_payload_inlines_text_large_blob(ctx: ServerContext) -> None:
    """text-large payloads spill to the object store; lookup-mode reads
    them back so the caller sees one consistent shape with payload.text
    populated."""
    ctx.inline_text_max_bytes = 100
    big = "Q" * 5000
    r = handlers.remember(content=TextContent(type="text", text=big))

    full = handlers.recall(by_id=r.event_id)
    assert len(full.hits) == 1
    hit = full.hits[0]
    assert hit.payload["content_type"] == "text-large"
    assert hit.payload["text"] == big
    assert hit.payload["blob_hash"].startswith("sha256:")
    assert hit.payload["size_bytes"] == len(big)


def test_recall_binary_returns_metadata_not_bytes(ctx: ServerContext) -> None:
    """Binary events keep raw bytes in the object store; recall surfaces
    metadata only. A future blob-fetch capability would expose actual bytes."""
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
    full = handlers.recall(by_id=r.event_id)
    assert len(full.hits) == 1
    payload = full.hits[0].payload
    assert payload["content_type"] == "binary"
    assert payload["mime"] == "image/png"
    assert payload["filename_hint"] == "bug.png"
    assert payload["size_bytes"] == len(raw)
    assert "text" not in payload  # bytes aren't inlined
