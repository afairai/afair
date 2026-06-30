"""Extractor agent tests — mocked LLM, no live network calls.

Live-LLM smoke is gated behind AFAIR_LIVE_LLM=1; off by default so
CI stays deterministic and unit tests stay fast.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from afair.agents import extractor, interpretation
from afair.agents.llm import LLMRateLimit, LLMResponseError, LLMResult, LLMTimeout
from afair.mcp import handlers
from afair.mcp.context import ServerContext, clear_context, set_context
from afair.mcp.schemas import ObserveEvent, TextContent
from afair.substrate import open_db

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class _KeyCtx:
    """Minimal stand-in for ServerContext, just the provider key attributes."""

    def __init__(self) -> None:
        self.openai_api_key = SecretStr("sk-openai")
        self.anthropic_api_key = SecretStr("sk-anthropic")
        self.gemini_api_key = SecretStr("sk-gemini")
        self.voyage_api_key = SecretStr("sk-voyage")


def test_api_key_for_managed_providers() -> None:
    ctx = _KeyCtx()
    assert extractor._api_key_for("openai/gpt-4o-mini", ctx) == "sk-openai"
    assert extractor._api_key_for("anthropic/claude-haiku-4-5", ctx) == "sk-anthropic"
    assert extractor._api_key_for("gemini/gemini-2.5-flash", ctx) == "sk-gemini"
    assert extractor._api_key_for("voyage/voyage-3", ctx) == "sk-voyage"


def test_api_key_for_self_authenticating_and_local_return_none() -> None:
    # github_copilot self-authenticates; ollama/fastembed need no key; unknown
    # providers are left to litellm's own env-var resolution. Critically, none
    # of these must be handed afair's anthropic key by mistake.
    ctx = _KeyCtx()
    for model in (
        "github_copilot/gpt-4o",
        "ollama/qwen2.5:7b",
        "fastembed/BAAI/bge-small-en-v1.5",
        "groq/llama-3.3-70b",
        "mistral/mistral-large",
    ):
        assert extractor._api_key_for(model, ctx) is None, model


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

    monkeypatch.setattr("afair.agents.extractor.call_tool", fake_call)


def _patch_llm_raises(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    """Replace the LLM call with one that raises ``exc``."""

    def fake_call(**_: object) -> LLMResult:
        raise exc

    monkeypatch.setattr("afair.agents.extractor.call_tool", fake_call)


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
    from afair.substrate import write_event

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
    from afair.substrate import write_event

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
    from afair.substrate import write_event

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
    from afair.substrate import write_event

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
    from afair.substrate import write_event

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


def test_extract_pdf_routes_through_pypdf(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A binary event with mime=application/pdf:
    pypdf extracts text, the text-LLM call receives it via user_message."""
    _patch_llm(monkeypatch, GOOD_EXTRACTION)

    # Capture what extract_pdf_text was called with + what build_user_message saw.
    seen_text: list[str | None] = []

    monkeypatch.setattr(
        "afair.agents.extractor.extract_pdf_text",
        lambda _path: "PDF body extracted by mock",
    )

    real_build = extractor.build_user_message

    def spy_build_user_message(event: Any, *, extracted_text: str | None = None) -> str:
        seen_text.append(extracted_text)
        return real_build(event, extracted_text=extracted_text)

    monkeypatch.setattr("afair.agents.extractor.build_user_message", spy_build_user_message)

    from afair.substrate import write_event, write_object

    blob_hash = write_object(ctx.vault_dir, b"%PDF-1.4 fake pdf bytes")
    e = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={
            "content_type": "binary",
            "blob_hash": blob_hash,
            "mime": "application/pdf",
            "size_bytes": 25,
            "filename_hint": "report.pdf",
            "context": "uploaded report",
            "type_hint": None,
        },
    )
    extractor.extract_sync(e.id)

    assert _count_interpretations(ctx) == 1
    extraction = _load_only_interpretation(ctx)
    assert extraction["status"] == "success"
    # extracted_text persisted on the interpretation row
    assert extraction["extracted_text"] == "PDF body extracted by mock"
    # build_user_message received the extracted text
    assert seen_text == ["PDF body extracted by mock"]

    # FTS enrichment: the extractor's summary + salient_facts + the
    # extracted PDF body should all be findable via events_fts now,
    # not just the filename + mime metadata that write_event indexed.
    from afair.substrate.search import search_fts

    by_summary = search_fts(ctx.db, "Sajinth")
    assert any(h.content_hash == e.content_hash for h in by_summary), (
        "FTS should find the PDF event via the extractor's summary"
    )
    by_body = search_fts(ctx.db, "extracted by mock")
    assert any(h.content_hash == e.content_hash for h in by_body), (
        "FTS should find the PDF event via the extracted body text"
    )


def test_extract_audio_routes_through_whisper(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A binary event with mime=audio/*:
    transcribe_audio runs, text-LLM call receives transcript via user_message."""
    _patch_llm(monkeypatch, GOOD_EXTRACTION)
    monkeypatch.setattr(
        "afair.agents.extractor.transcribe_audio",
        lambda **_: "transcribed audio content",
    )

    from afair.substrate import write_event, write_object

    blob_hash = write_object(ctx.vault_dir, b"fake-audio-bytes")
    e = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={
            "content_type": "binary",
            "blob_hash": blob_hash,
            "mime": "audio/mpeg",
            "size_bytes": 16,
            "filename_hint": "memo.mp3",
            "context": None,
            "type_hint": None,
        },
    )
    extractor.extract_sync(e.id)

    extraction = _load_only_interpretation(ctx)
    assert extraction["status"] == "success"
    assert extraction["extracted_text"] == "transcribed audio content"


def test_extract_image_routes_through_vision(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A binary event with mime=image/*:
    describe_image runs (NOT the text-LLM call) and returns directly."""

    def fake_call_tool(**_: object) -> LLMResult:
        # This MUST NOT be reached for image events.
        msg = "text-LLM call_tool reached on image event"
        raise AssertionError(msg)

    monkeypatch.setattr("afair.agents.extractor.call_tool", fake_call_tool)
    monkeypatch.setattr(
        "afair.agents.extractor.describe_image",
        lambda **_: GOOD_EXTRACTION,
    )

    from afair.substrate import write_event, write_object

    blob_hash = write_object(ctx.vault_dir, b"PNG fake bytes")
    e = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={
            "content_type": "binary",
            "blob_hash": blob_hash,
            "mime": "image/png",
            "size_bytes": 14,
            "filename_hint": "screenshot.png",
            "context": "from claude code",
            "type_hint": None,
        },
    )
    extractor.extract_sync(e.id)

    extraction = _load_only_interpretation(ctx)
    assert extraction["status"] == "success"
    assert extraction["best_guess_kind"] == "email"  # from GOOD_EXTRACTION


def test_extract_pdf_failure_writes_failed_interpretation(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from afair.agents.binary_extractors import PdfExtractionError

    def fail_pdf(_path: Any) -> str:
        msg = "corrupt PDF"
        raise PdfExtractionError(msg)

    monkeypatch.setattr("afair.agents.extractor.extract_pdf_text", fail_pdf)

    from afair.substrate import write_event, write_object

    blob_hash = write_object(ctx.vault_dir, b"garbage")
    e = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={
            "content_type": "binary",
            "blob_hash": blob_hash,
            "mime": "application/pdf",
            "size_bytes": 7,
            "context": None,
            "type_hint": None,
        },
    )
    extractor.extract_sync(e.id)

    extraction = _load_only_interpretation(ctx)
    assert extraction["status"] == "failed"
    assert extraction["error_type"] == "pdf_extraction_error"
    assert "corrupt PDF" in extraction["error_message"]


def test_remember_handler_triggers_extraction(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calling remember() through the handler schedules+runs extraction.

    We patch schedule_extraction to run synchronously so the test is
    deterministic without polling the thread pool.
    """
    _patch_llm(monkeypatch, GOOD_EXTRACTION)
    monkeypatch.setattr(
        "afair.mcp.handlers.schedule_extraction",
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

    monkeypatch.setattr("afair.mcp.handlers.schedule_extraction", counting_sync)
    handlers.remember(content=TextContent(type="text", text="same content"))
    handlers.remember(content=TextContent(type="text", text="same content"))
    assert call_count["n"] == 1


def test_observe_handler_triggers_extraction(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_llm(monkeypatch, GOOD_EXTRACTION)
    monkeypatch.setattr(
        "afair.mcp.handlers.schedule_extraction",
        extractor.extract_sync,
    )
    handlers.observe(event=ObserveEvent(action="edit_file", subject="x.py"))
    assert _count_interpretations(ctx) == 1


# ── prompt construction ────────────────────────────────────────────────────


def test_user_message_truncates_over_long_text(ctx: ServerContext) -> None:
    """A 60KB text field gets truncated with a marker so the LLM stays in budget."""
    from afair.agents.prompts import (
        MAX_USER_MESSAGE_CHARS,
        build_user_message,
    )
    from afair.substrate import write_event

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
    from afair.agents.prompts import build_user_message
    from afair.substrate import write_event

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
    from afair.agents.prompts import EXTRACTOR_TOOL_SCHEMA

    required = EXTRACTOR_TOOL_SCHEMA["required"]
    assert "best_guess_kind" in required
    assert "summary" in required
    # Every property defined has a description so the model knows what to put.
    for name, defn in EXTRACTOR_TOOL_SCHEMA["properties"].items():
        assert "description" in defn or "type" in defn, f"property {name} has neither"


def test_user_message_includes_text_for_inline_text(ctx: ServerContext) -> None:
    from afair.agents.prompts import build_user_message
    from afair.substrate import write_event

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
    from afair.agents.prompts import build_user_message
    from afair.substrate import write_event

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
    from afair.mcp import handlers
    from afair.mcp.schemas import TextContent

    _patch_llm(monkeypatch, GOOD_EXTRACTION)
    monkeypatch.setattr(
        "afair.mcp.handlers.schedule_extraction",
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
    from afair.mcp import handlers
    from afair.mcp.schemas import TextContent

    _patch_llm_raises(monkeypatch, LLMTimeout("upstream timed out"))
    monkeypatch.setattr(
        "afair.mcp.handlers.schedule_extraction",
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
    from afair.agents.binder import find_and_record_links, get_linked_event_ids
    from afair.substrate import write_event

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
    from afair.agents.binder import find_and_record_links, get_linked_event_ids
    from afair.substrate import write_event

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
    from afair.agents.binder import find_and_record_links
    from afair.mcp import handlers
    from afair.mcp.schemas import TextContent

    _patch_llm(monkeypatch, GOOD_EXTRACTION)
    # Skip the embedding+binder path inside extract_sync — we'll set up
    # the bind record manually for determinism.
    monkeypatch.setattr(
        "afair.mcp.handlers.schedule_extraction",
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

    from afair.substrate import iter_events

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
    from afair.substrate import write_event

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
    os.environ.get("AFAIR_LIVE_LLM") != "1",
    reason="set AFAIR_LIVE_LLM=1 to hit the real Anthropic API",
)
def test_live_extraction_against_anthropic(tmp_path: Path) -> None:
    """End-to-end smoke against the real Anthropic API. Costs ~1¢, skipped by default."""
    from afair.settings import load_settings
    from afair.substrate import write_event

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


# ── text-large lifecycle (>inline-threshold text that spills to a blob) ──────
#
# Regression guard for the spill gap: a text payload above
# inline_text_max_bytes spills to the object store, so the canonical event
# row carries only a blob_hash. Without explicit handling that body would be
# invisible to FTS (write-time) and to the extractor + embedding (cold path).


def _spilled_text_payload(ctx: ServerContext, body: str) -> dict[str, Any]:
    from afair.substrate.payload import build_text_payload

    payload = build_text_payload(
        text=body,
        context=None,
        type_hint=None,
        vault_dir=ctx.vault_dir,
        inline_text_max_bytes=ctx.inline_text_max_bytes,
    )
    assert payload["content_type"] == "text-large"  # actually spilled
    assert "text" not in payload  # body is NOT inline in the canonical row
    return payload


def test_text_large_body_indexed_in_fts_at_write_time(ctx: ServerContext) -> None:
    """The spilled body reaches FTS immediately via searchable_body — no
    dependency on the cold path. A large paste is findable the moment it's
    written."""
    from afair.substrate import write_event

    marker = "zylophthalmic"  # distinctive token that only the body contains
    body = ("filler sentence for bulk. " * 5000) + marker
    assert len(body.encode("utf-8")) > ctx.inline_text_max_bytes

    payload = _spilled_text_payload(ctx, body)
    write_event(ctx.db, origin="user", kind="remember", payload=payload, searchable_body=body)

    hits = ctx.db.execute(
        "SELECT COUNT(*) FROM events_fts WHERE events_fts MATCH ?", (marker,)
    ).fetchone()[0]
    assert hits == 1


def test_text_large_without_searchable_body_is_invisible_to_fts(ctx: ServerContext) -> None:
    """Documents the bug the override fixes: a spilled body written WITHOUT
    searchable_body is unfindable by its contents — proving the override is
    load-bearing, not cosmetic."""
    from afair.substrate import write_event

    marker = "phlogisticum"
    body = ("filler sentence for bulk. " * 5000) + marker
    payload = _spilled_text_payload(ctx, body)
    write_event(ctx.db, origin="user", kind="remember", payload=payload)  # no override

    hits = ctx.db.execute(
        "SELECT COUNT(*) FROM events_fts WHERE events_fts MATCH ?", (marker,)
    ).fetchone()[0]
    assert hits == 0


def test_extractor_rehydrates_text_large_body(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The extractor reads the spilled blob back, so the body reaches the
    interpretation (hence embedding) and survives the FTS enrich step."""
    _patch_llm(monkeypatch, GOOD_EXTRACTION)
    from afair.substrate import write_event

    marker = "qwizzlefemt"
    body = ("lorem ipsum dolor sit amet. " * 6000) + marker
    payload = _spilled_text_payload(ctx, body)
    e = write_event(ctx.db, origin="user", kind="remember", payload=payload, searchable_body=body)

    extractor.extract_sync(e.id)

    extraction = _load_only_interpretation(ctx)
    assert extraction["status"] == "success"
    # Rehydrated body was stashed on the interpretation → embedding + enrich saw it.
    assert marker in extraction["extracted_text"]
    # And the enrich step (DELETE+INSERT on events_fts) preserved the body.
    hits = ctx.db.execute(
        "SELECT COUNT(*) FROM events_fts WHERE events_fts MATCH ?", (marker,)
    ).fetchone()[0]
    assert hits == 1
