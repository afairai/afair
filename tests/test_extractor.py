"""Extractor agent tests — mocked LLM, no live network calls.

Live-LLM smoke is gated behind NEVERFORGET_LIVE_LLM=1; off by default so
CI stays deterministic and unit tests stay fast.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

import pytest

from neverforget.agents import extractor, interpretation
from neverforget.agents.llm import LLMRateLimit, LLMResponseError, LLMResult, LLMTimeout
from neverforget.mcp import handlers
from neverforget.mcp.context import ServerContext, clear_context, set_context
from neverforget.mcp.schemas import ObserveEvent, TextContent
from neverforget.substrate import open_db

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


# A canonical "good" extraction the mock LLM returns by default.
GOOD_EXTRACTION: dict[str, Any] = {
    "best_guess_kind": "email",
    "summary": "Sajinth shared the new roadmap focused on memory.",
    "entities": [
        {"name": "Sajinth", "type": "person"},
        {"name": "roadmap", "type": "concept"},
    ],
    "relations": [{"subject": "Sajinth", "predicate": "proposed", "object": "new roadmap"}],
    "time_references": [],
    "salient_facts": ["new roadmap focused on memory"],
    "language": "en",
    "confidence": 0.85,
    "source_attribution": "Sajinth",
}


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[ServerContext]:
    db = open_db(tmp_path)
    sc = ServerContext(
        db=db,
        vault_dir=tmp_path,
        inline_text_max_bytes=64 * 1024,
        # No real API keys in unit tests — call_json is mocked.
        anthropic_api_key=None,
    )
    set_context(sc)
    try:
        yield sc
    finally:
        db.close()
        clear_context()


# ── helpers ─────────────────────────────────────────────────────────────────


def _patch_llm(monkeypatch: pytest.MonkeyPatch, response: dict[str, Any]) -> None:
    """Replace the LLM call with one that returns a fixed dict."""

    def fake_call(**_: object) -> LLMResult:
        return LLMResult(
            data=response,
            model="mock",
            raw=json.dumps(response),
        )

    monkeypatch.setattr("neverforget.agents.extractor.call_tool", fake_call)


def _patch_llm_raises(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    """Replace the LLM call with one that raises ``exc``."""

    def fake_call(**_: object) -> LLMResult:
        raise exc

    monkeypatch.setattr("neverforget.agents.extractor.call_tool", fake_call)


def _count_interpretations(ctx: ServerContext) -> int:
    return ctx.db.execute("SELECT COUNT(*) FROM interpretations").fetchone()[0]


def _load_only_interpretation(ctx: ServerContext) -> dict[str, Any]:
    row = ctx.db.execute("SELECT extraction FROM interpretations").fetchone()
    return json.loads(row["extraction"])


# ── extraction success ─────────────────────────────────────────────────────


def test_extract_sync_writes_successful_interpretation(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_llm(monkeypatch, GOOD_EXTRACTION)
    # Write an event directly (bypass handler) then run extraction.
    from neverforget.substrate import write_event

    e = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={
            "content_type": "text",
            "text": "Sajinth proposed a new roadmap",
            "context": "email",
            "type_hint": None,
        },
    )
    extractor.extract_sync(e.id)

    assert _count_interpretations(ctx) == 1
    extraction = _load_only_interpretation(ctx)
    assert extraction["status"] == "success"
    assert extraction["best_guess_kind"] == "email"
    assert extraction["entities"][0]["name"] == "Sajinth"


# ── extraction failures (option (b): failed row is written) ─────────────────


def test_extract_llm_timeout_writes_failed_interpretation(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_llm_raises(monkeypatch, LLMTimeout("upstream timed out"))
    from neverforget.substrate import write_event

    e = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "x", "context": None, "type_hint": None},
    )
    extractor.extract_sync(e.id)

    extraction = _load_only_interpretation(ctx)
    assert extraction["status"] == "failed"
    assert extraction["error_type"] == "llm_timeout"
    assert "timed out" in extraction["error_message"]
    assert "attempted_at" in extraction


def test_extract_rate_limit_writes_failed_interpretation(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_llm_raises(monkeypatch, LLMRateLimit("rate limited"))
    from neverforget.substrate import write_event

    e = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "x", "context": None, "type_hint": None},
    )
    extractor.extract_sync(e.id)

    extraction = _load_only_interpretation(ctx)
    assert extraction["status"] == "failed"
    assert extraction["error_type"] == "llm_rate_limit"


def test_extract_malformed_response_writes_failed_interpretation(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_llm_raises(monkeypatch, LLMResponseError("non-JSON response: line 1 col 1 char 0"))
    from neverforget.substrate import write_event

    e = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "x", "context": None, "type_hint": None},
    )
    extractor.extract_sync(e.id)

    extraction = _load_only_interpretation(ctx)
    assert extraction["status"] == "failed"
    assert extraction["error_type"] == "llm_response_error"


def test_extract_missing_required_field_writes_failed_interpretation(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Returns JSON but missing best_guess_kind → validation failure.
    bad = {"summary": "..."}
    _patch_llm(monkeypatch, bad)
    from neverforget.substrate import write_event

    e = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "x", "context": None, "type_hint": None},
    )
    extractor.extract_sync(e.id)

    extraction = _load_only_interpretation(ctx)
    assert extraction["status"] == "failed"
    assert "best_guess_kind" in extraction["error_message"]


# ── handler → extractor integration ────────────────────────────────────────


def test_remember_handler_triggers_extraction(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calling remember() through the handler schedules+runs extraction.

    We patch schedule_extraction to run synchronously so the test is
    deterministic without polling the thread pool.
    """
    _patch_llm(monkeypatch, GOOD_EXTRACTION)
    monkeypatch.setattr(
        "neverforget.mcp.handlers.schedule_extraction",
        extractor.extract_sync,
    )
    handlers.remember(
        content=TextContent(type="text", text="Sajinth proposed a new roadmap"),
        context="email",
    )
    assert _count_interpretations(ctx) == 1


def test_dedup_does_not_re_extract(ctx: ServerContext, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second identical remember does not trigger re-extraction.

    The existing event already had its extraction window; running it again
    would waste an LLM call. Future Phase X consolidator may revisit.
    """
    _patch_llm(monkeypatch, GOOD_EXTRACTION)
    call_count = {"n": 0}

    def counting_sync(event_id: str) -> None:
        call_count["n"] += 1
        extractor.extract_sync(event_id)

    monkeypatch.setattr("neverforget.mcp.handlers.schedule_extraction", counting_sync)
    handlers.remember(content=TextContent(type="text", text="same content"))
    handlers.remember(content=TextContent(type="text", text="same content"))
    assert call_count["n"] == 1


def test_observe_handler_triggers_extraction(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_llm(monkeypatch, GOOD_EXTRACTION)
    monkeypatch.setattr(
        "neverforget.mcp.handlers.schedule_extraction",
        extractor.extract_sync,
    )
    handlers.observe(event=ObserveEvent(action="edit_file", subject="x.py"))
    assert _count_interpretations(ctx) == 1


# ── prompt construction ────────────────────────────────────────────────────


def test_user_message_truncates_over_long_text(ctx: ServerContext) -> None:
    """A 60KB text field gets truncated with a marker so the LLM stays in budget."""
    from neverforget.agents.prompts import (
        MAX_USER_MESSAGE_CHARS,
        build_user_message,
    )
    from neverforget.substrate import write_event

    big = "abcde" * 12_000  # 60_000 chars, well above MAX_USER_MESSAGE_CHARS (30_000)
    assert len(big) > MAX_USER_MESSAGE_CHARS
    e = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": big, "context": None, "type_hint": None},
    )
    msg = build_user_message(e)

    # The full raw text must NOT appear verbatim in the LLM message.
    assert big not in msg
    # Elision marker is present and gives the LLM enough info to know it's truncated.
    assert "TRUNCATED" in msg
    assert "elided" in msg
    # The original length is surfaced so future-us can debug.
    assert "truncated_original_length" in msg
    # Head + tail markers are preserved (first and last chars present).
    assert big[:50] in msg
    assert big[-50:] in msg


def test_user_message_for_normal_size_is_not_truncated(ctx: ServerContext) -> None:
    """Below the threshold, the text passes through untouched."""
    from neverforget.agents.prompts import build_user_message
    from neverforget.substrate import write_event

    normal = "hello world " * 100  # ~1200 chars, well under threshold
    e = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": normal, "context": None, "type_hint": None},
    )
    msg = build_user_message(e)
    assert normal in msg
    assert "TRUNCATED" not in msg


def test_tool_schema_required_fields_present() -> None:
    """The schema we ship to the model must include the mandatory fields."""
    from neverforget.agents.prompts import EXTRACTOR_TOOL_SCHEMA

    required = EXTRACTOR_TOOL_SCHEMA["required"]
    assert "best_guess_kind" in required
    assert "summary" in required
    # Every property defined has a description so the model knows what to put.
    for name, defn in EXTRACTOR_TOOL_SCHEMA["properties"].items():
        assert "description" in defn or "type" in defn, f"property {name} has neither"


def test_user_message_includes_text_for_inline_text(ctx: ServerContext) -> None:
    from neverforget.agents.prompts import build_user_message
    from neverforget.substrate import write_event

    e = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={
            "content_type": "text",
            "text": "memorable text",
            "context": "test",
            "type_hint": None,
        },
    )
    msg = build_user_message(e)
    assert "memorable text" in msg
    assert "test" in msg


def test_user_message_for_binary_uses_metadata_only(ctx: ServerContext) -> None:
    from neverforget.agents.prompts import build_user_message
    from neverforget.substrate import write_event

    e = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={
            "content_type": "binary",
            "blob_hash": "sha256:abc",
            "mime": "image/png",
            "size_bytes": 1234,
            "filename_hint": "screenshot.png",
            "context": "bug hunt",
        },
    )
    msg = build_user_message(e)
    # Blob hash is NOT exposed to the LLM (we send only metadata for now)
    assert "sha256:abc" not in msg
    assert "image/png" in msg
    assert "screenshot.png" in msg
    assert "bug hunt" in msg


# ── recall surfaces interpretation (A2) ────────────────────────────────────


def test_recall_surfaces_interpretation_when_present(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A2: recall hits include the latest successful Extractor output."""
    from neverforget.mcp import handlers
    from neverforget.mcp.schemas import TextContent

    _patch_llm(monkeypatch, GOOD_EXTRACTION)
    monkeypatch.setattr(
        "neverforget.mcp.handlers.schedule_extraction",
        extractor.extract_sync,
    )
    handlers.remember(
        content=TextContent(type="text", text="Sajinth proposed a new roadmap"),
        context="email thread",
    )
    r = handlers.recall(query="Sajinth", depth="shallow")
    assert len(r.hits) == 1
    interp = r.hits[0].interpretation
    assert interp is not None
    assert interp["best_guess_kind"] == "email"
    # entities surface through verbatim
    names = [e["name"] for e in interp["entities"]]
    assert "Sajinth" in names


def test_recall_omits_interpretation_when_extraction_failed(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed extractions don't surface — recall hits return interpretation=None."""
    from neverforget.mcp import handlers
    from neverforget.mcp.schemas import TextContent

    _patch_llm_raises(monkeypatch, LLMTimeout("upstream timed out"))
    monkeypatch.setattr(
        "neverforget.mcp.handlers.schedule_extraction",
        extractor.extract_sync,
    )
    handlers.remember(content=TextContent(type="text", text="anything"))
    r = handlers.recall(query="anything", depth="shallow")
    assert len(r.hits) == 1
    assert r.hits[0].interpretation is None


# ── binder v0 (A4) ─────────────────────────────────────────────────────────


def test_binder_links_semantically_similar_events(ctx: ServerContext) -> None:
    """Three events with similar embeddings — binder should link them
    in each direction."""
    from neverforget.agents.binder import find_and_record_links, get_linked_event_ids
    from neverforget.substrate import write_event

    # Build three events with controlled embeddings (3-dim for simplicity).
    e1 = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "alpha", "context": None},
    )
    e2 = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "beta", "context": None},
    )
    e3 = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "gamma", "context": None},
    )

    # NOTE: the test DB was opened with embedding_dim=1536 (the default).
    # Re-create the vec table at dim=3 so this test is fast.
    ctx.db.execute("DROP TABLE IF EXISTS events_vec")
    ctx.db.execute(
        "CREATE VIRTUAL TABLE events_vec USING vec0(content_hash TEXT PRIMARY KEY, embedding FLOAT[3])"
    )

    import struct

    # Make e1, e2 similar (along +x axis); e3 different (along +y axis).
    for ch, vec in [
        (e1.content_hash, [1.0, 0.0, 0.0]),
        (e2.content_hash, [0.9, 0.1, 0.0]),  # close to e1
        (e3.content_hash, [0.0, 1.0, 0.0]),  # far from e1
    ]:
        ctx.db.execute(
            "INSERT INTO events_vec(content_hash, embedding) VALUES (?, ?)",
            (ch, struct.pack("<3f", *vec)),
        )

    # Link e1 against the others — should find e2 closest.
    result = find_and_record_links(ctx.db, event=e1, embedding=[1.0, 0.0, 0.0], top_k=2)
    assert result is not None
    link_hashes = [link["event_hash"] for link in result["links"]]
    # e2 should be first (most similar to e1)
    assert link_hashes[0] == e2.content_hash
    # e1 itself must be filtered out
    assert e1.content_hash not in link_hashes

    # Helper round-trip
    linked = get_linked_event_ids(ctx.db, e1.content_hash)
    assert e2.content_hash in linked


def test_binder_skips_when_no_neighbors(ctx: ServerContext) -> None:
    """A single isolated event has no neighbors → binder records nothing."""
    from neverforget.agents.binder import find_and_record_links, get_linked_event_ids
    from neverforget.substrate import write_event

    e = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "lonely", "context": None},
    )
    # No events_vec row for this event — query returns 0 neighbors.
    result = find_and_record_links(ctx.db, event=e, embedding=[1.0] * 1536)
    assert result is None
    assert get_linked_event_ids(ctx.db, e.content_hash) == []


def test_recall_surfaces_linked_event_ids(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RecallHit carries linked_event_ids when the Bind agent has run."""
    from neverforget.agents.binder import find_and_record_links
    from neverforget.mcp import handlers
    from neverforget.mcp.schemas import TextContent

    _patch_llm(monkeypatch, GOOD_EXTRACTION)
    # Skip the embedding+binder path inside extract_sync — we'll set up
    # the bind record manually for determinism.
    monkeypatch.setattr(
        "neverforget.mcp.handlers.schedule_extraction",
        lambda _id: None,
    )

    handlers.remember(content=TextContent(type="text", text="primary"))
    handlers.remember(content=TextContent(type="text", text="neighbor"))

    # Manually re-create the vec table at dim=3 for the test, populate,
    # and run the binder.
    ctx.db.execute("DROP TABLE IF EXISTS events_vec")
    ctx.db.execute(
        "CREATE VIRTUAL TABLE events_vec USING vec0(content_hash TEXT PRIMARY KEY, embedding FLOAT[3])"
    )
    import struct

    from neverforget.substrate import iter_events

    events = list(iter_events(ctx.db, kind="remember", order="asc"))
    for ev, vec in zip(events, [[1.0, 0.0, 0.0], [0.9, 0.1, 0.0]], strict=False):
        ctx.db.execute(
            "INSERT INTO events_vec(content_hash, embedding) VALUES (?, ?)",
            (ev.content_hash, struct.pack("<3f", *vec)),
        )
    find_and_record_links(ctx.db, event=events[0], embedding=[1.0, 0.0, 0.0], top_k=2)

    r = handlers.recall(query="primary", depth="shallow")
    assert len(r.hits) == 1
    hit = r.hits[0]
    assert events[1].content_hash in hit.linked_event_ids


# ── interpretation idempotency ─────────────────────────────────────────────


def test_interpretation_write_is_idempotent(ctx: ServerContext) -> None:
    """Running the same extractor twice on the same event produces one row."""
    from neverforget.substrate import write_event

    e = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "x", "context": None, "type_hint": None},
    )
    i1 = interpretation.write_interpretation(
        ctx.db,
        event=e,
        version=1,
        produced_by="extractor:mock",
        extraction={"status": "success", "best_guess_kind": "x", "summary": "x"},
    )
    i2 = interpretation.write_interpretation(
        ctx.db,
        event=e,
        version=1,
        produced_by="extractor:mock",
        extraction={"status": "success", "best_guess_kind": "y", "summary": "y"},
    )
    # Same (event_hash, version, produced_by) → same row returned, no overwrite
    assert i1.id == i2.id
    assert _count_interpretations(ctx) == 1


# ── live LLM smoke (opt-in via env var) ────────────────────────────────────


@pytest.mark.skipif(
    os.environ.get("NEVERFORGET_LIVE_LLM") != "1",
    reason="set NEVERFORGET_LIVE_LLM=1 to hit the real Anthropic API",
)
def test_live_extraction_against_anthropic(tmp_path: Path) -> None:
    """End-to-end smoke against the real Anthropic API. Costs ~1¢, skipped by default."""
    from neverforget.settings import load_settings
    from neverforget.substrate import write_event

    settings = load_settings()
    assert settings.anthropic_api_key is not None, "needs ANTHROPIC_API_KEY in .env.local"

    db = open_db(tmp_path)
    set_context(
        ServerContext(
            db=db,
            vault_dir=tmp_path,
            inline_text_max_bytes=64 * 1024,
            extractor_model=settings.extractor_model,
            anthropic_api_key=settings.anthropic_api_key,
        )
    )
    try:
        e = write_event(
            db,
            origin="user",
            kind="remember",
            payload={
                "content_type": "text",
                "text": "Sajinth proposed a new roadmap focused on memory.",
                "context": "email from Sajinth, 2026-05-25",
                "type_hint": "email",
            },
        )
        extractor.extract_sync(e.id)

        row = db.execute("SELECT extraction FROM interpretations").fetchone()
        assert row is not None
        ext = json.loads(row["extraction"])
        assert ext["status"] == "success"
        assert isinstance(ext["best_guess_kind"], str)
        assert "Sajinth" in str(ext).lower() or "sajinth" in str(ext).lower()
    finally:
        db.close()
        clear_context()
