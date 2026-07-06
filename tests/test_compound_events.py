"""Atomic compound-event tests (Tier 3 of the binary-content audit).

A compound event groups multiple payloads (text + blob refs) under one
content_hash. Test that:
  * remember(CompoundContent) writes one event row
  * FTS indexes all parts' text + labels + blob metadata
  * recall surfaces the parts inline
  * dangling blob refs are rejected (no half-written compound rows)
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from afair.mcp.context import clear_context
from afair.mcp.schemas import (
    CompoundBlobRefPart,
    CompoundContent,
    CompoundTextPart,
)
from afair.mcp.server import build_app
from afair.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


SAMPLE_TOKEN = "test-token-do-not-use-in-production"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(
        "afair.mcp.handlers.schedule_extraction",
        lambda _event_id: None,
    )
    clear_context()
    try:
        yield
    finally:
        clear_context()


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=tmp_path,
        auth_token=SAMPLE_TOKEN,  # type: ignore[arg-type]
    )


def _client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(_settings(tmp_path)))


# ── happy path ─────────────────────────────────────────────────────────────


def test_compound_text_only_creates_one_event(tmp_path: Path) -> None:
    """Three text parts → one event row."""
    from afair.mcp import handlers
    from afair.mcp.context import connect_for_thread

    with _client(tmp_path):
        result = handlers.remember(
            content=CompoundContent(
                type="compound",
                parts=[
                    CompoundTextPart(type="text", text="Meeting notes", label="notes"),
                    CompoundTextPart(type="text", text="Decision made", label="decision"),
                    CompoundTextPart(type="text", text="Action items: ship", label="actions"),
                ],
            ),
            context="weekly sync",
        )
        assert result.ok is True

        db = connect_for_thread()
        row = db.execute("SELECT payload FROM events WHERE id = ?", (result.event_id,)).fetchone()
        payload = json.loads(row["payload"])
        assert payload["content_type"] == "compound"
        assert len(payload["parts"]) == 3
        assert {p["label"] for p in payload["parts"]} == {"notes", "decision", "actions"}
        # Total event count
        count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert count == 1


def test_compound_with_blob_ref_part(tmp_path: Path) -> None:
    """Compound containing a blob-ref: blob must exist; size populated."""
    bytes_ = b"fake PDF body" * 200

    with _client(tmp_path) as client:
        upload = client.post(
            "/internal/blob/upload",
            content=bytes_,
            headers={
                "Authorization": f"Bearer {SAMPLE_TOKEN}",
                "Content-Type": "application/octet-stream",
            },
        )
        blob_hash = upload.json()["blob_hash"]

        from afair.mcp import handlers
        from afair.mcp.context import connect_for_thread

        result = handlers.remember(
            content=CompoundContent(
                type="compound",
                parts=[
                    CompoundTextPart(
                        type="text",
                        text="Transcript of the meeting",
                        label="transcript",
                    ),
                    CompoundBlobRefPart(
                        type="blob-ref",
                        blob_hash=blob_hash,
                        mime="application/pdf",
                        filename_hint="slides.pdf",
                        label="slides",
                    ),
                ],
            ),
            context="meeting bundle",
        )
        db = connect_for_thread()
        row = db.execute("SELECT payload FROM events WHERE id = ?", (result.event_id,)).fetchone()
        payload = json.loads(row["payload"])
        assert payload["content_type"] == "compound"
        parts = payload["parts"]
        assert parts[0]["type"] == "text"
        assert parts[0]["text"] == "Transcript of the meeting"
        assert parts[1]["type"] == "blob-ref"
        assert parts[1]["blob_hash"] == blob_hash
        assert parts[1]["size_bytes"] == len(bytes_)
        assert parts[1]["label"] == "slides"


def test_compound_fts_indexes_all_parts(tmp_path: Path) -> None:
    """An FTS keyword search hits text from any compound part."""
    from afair.mcp import handlers
    from afair.mcp.context import connect_for_thread
    from afair.substrate.search import search_fts

    with _client(tmp_path):
        result = handlers.remember(
            content=CompoundContent(
                type="compound",
                parts=[
                    CompoundTextPart(type="text", text="afair memory layer", label="title"),
                    CompoundTextPart(
                        type="text",
                        text="Sajinth proposed dogfooding next week",
                        label="quote",
                    ),
                ],
            ),
            context="bundle",
        )

        db = connect_for_thread()
        hits_by_first = search_fts(db, "afair")
        assert any(h.content_hash == result.content_hash for h in hits_by_first)
        hits_by_second = search_fts(db, "Sajinth")
        assert any(h.content_hash == result.content_hash for h in hits_by_second)


# ── rejection paths ────────────────────────────────────────────────────────


def test_compound_with_missing_blob_ref_rejected(tmp_path: Path) -> None:
    from afair.mcp import handlers
    from afair.mcp.handlers import InvalidateTargetError

    with _client(tmp_path), pytest.raises(InvalidateTargetError, match="not found"):
        handlers.remember(
            content=CompoundContent(
                type="compound",
                parts=[
                    CompoundTextPart(type="text", text="caption"),
                    CompoundBlobRefPart(
                        type="blob-ref",
                        blob_hash="sha256:" + "0" * 64,
                        mime="image/png",
                    ),
                ],
            ),
            context="bad bundle",
        )


def test_compound_must_have_at_least_one_part() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CompoundContent(type="compound", parts=[])


def test_compound_capped_at_max_parts() -> None:
    from pydantic import ValidationError

    from afair.mcp.schemas import MAX_COMPOUND_PARTS

    too_many = [
        CompoundTextPart(type="text", text=f"part {i}") for i in range(MAX_COMPOUND_PARTS + 1)
    ]
    with pytest.raises(ValidationError):
        CompoundContent(type="compound", parts=too_many)


# ── recall surface ─────────────────────────────────────────────────────────


def test_compound_payload_full_lookup_returns_all_parts(tmp_path: Path) -> None:
    """recall(by_content_hash=...) returns full_payload — all parts
    intact, no truncation."""
    from afair.mcp import handlers

    with _client(tmp_path):
        long_text = "x" * 1000
        wrote = handlers.remember(
            content=CompoundContent(
                type="compound",
                parts=[
                    CompoundTextPart(type="text", text=long_text, label="big"),
                    CompoundTextPart(type="text", text="small", label="tiny"),
                ],
            ),
            context="mixed",
        )
        recall = handlers.recall(by_content_hash=wrote.content_hash)
        assert len(recall.hits) == 1
        payload_view = recall.hits[0].payload
        assert payload_view["content_type"] == "compound"
        parts = payload_view["parts"]
        assert len(parts) == 2
        # Lookup mode → full payload, so text is intact
        assert parts[0]["text"] == long_text
        assert parts[1]["text"] == "small"
        labels = [p.get("label") for p in parts]
        assert labels == ["big", "tiny"]


def test_compound_payload_summary_truncates_in_search_mode(tmp_path: Path) -> None:
    """A recall via query (NOT by_content_hash) returns the summary
    view — long part text is truncated to SUMMARY_TEXT_CHARS=500."""
    from afair.mcp import handlers

    with _client(tmp_path):
        long_text = "afairmemory " * 100  # ~1200 chars, contains "afairmemory" token
        wrote = handlers.remember(
            content=CompoundContent(
                type="compound",
                parts=[
                    CompoundTextPart(type="text", text=long_text, label="bulk"),
                ],
            ),
            context="searchable",
        )
        recall = handlers.recall(query="afairmemory", depth="shallow")
        matching = [h for h in recall.hits if h.content_hash == wrote.content_hash]
        assert matching, "FTS should have found the compound event"
        part = matching[0].payload["parts"][0]
        # Search-mode summary truncates per-part text to SUMMARY_TEXT_CHARS.
        assert len(part["text"]) <= 500


# ── P2b: per-part spill (oversized inline part) ─────────────────────────────


def _settings_small_inline(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=tmp_path,
        auth_token=SAMPLE_TOKEN,  # type: ignore[arg-type]
        inline_text_max_bytes=64,
    )


def test_compound_oversized_part_spills_to_blob_row_stays_small(tmp_path: Path) -> None:
    """A text part over inline_text_max_bytes spills to the object store as a
    ``text-large`` part; the SQLite row keeps a blob_hash, not the body, and
    FTS still finds a term from the spilled part."""
    from afair.mcp import handlers
    from afair.mcp.context import connect_for_thread
    from afair.substrate.search import search_fts

    with TestClient(build_app(_settings_small_inline(tmp_path))):
        big = "spilledneedle " * 40  # ~560 bytes > 64-byte inline cap
        result = handlers.remember(
            content=CompoundContent(
                type="compound",
                parts=[
                    CompoundTextPart(type="text", text="small inline", label="tiny"),
                    CompoundTextPart(type="text", text=big, label="bulk"),
                ],
            ),
            context="mixed",
        )
        db = connect_for_thread()
        row = db.execute("SELECT payload FROM events WHERE id = ?", (result.event_id,)).fetchone()
        payload = json.loads(row["payload"])
        parts = payload["parts"]
        # Inline part unchanged.
        assert parts[0]["type"] == "text"
        assert parts[0]["text"] == "small inline"
        # Oversized part spilled: no inline body, carries a blob_hash.
        assert parts[1]["type"] == "text-large"
        assert "text" not in parts[1]
        assert parts[1]["blob_hash"].startswith("sha256:")
        assert parts[1]["label"] == "bulk"
        # The row payload must not contain the spilled body.
        assert "spilledneedle" not in row["payload"]
        # FTS still finds the spilled term (routed via searchable_body).
        hits = search_fts(db, "spilledneedle")
        assert any(h.content_hash == result.content_hash for h in hits)


def test_compound_sum_of_parts_over_cap_rejected(tmp_path: Path) -> None:
    """Total text-part bytes over MAX_REMEMBER_BYTES → ContentTooLargeError,
    mirroring the single-text/binary v1 size lock."""
    from afair.mcp import handlers
    from afair.mcp.handlers import ContentTooLargeError
    from afair.mcp.schemas import MAX_COMPOUND_PARTS, MAX_REMEMBER_BYTES

    # Split just-over-cap total across the allowed part count.
    per_part = (MAX_REMEMBER_BYTES // MAX_COMPOUND_PARTS) + 1
    parts = [CompoundTextPart(type="text", text="a" * per_part) for _ in range(MAX_COMPOUND_PARTS)]
    with _client(tmp_path), pytest.raises(ContentTooLargeError, match="compound text parts total"):
        handlers.remember(content=CompoundContent(type="compound", parts=parts))


# ── P2b: build_user_message flattens parts into the truncation budget ───────


def test_build_user_message_compound_within_budget(tmp_path: Path) -> None:
    """A compound event's parts flatten into ``text`` and the whole message
    respects MAX_USER_MESSAGE_CHARS — previously ``parts`` entered the extras
    loop unbounded and blew the context window."""
    from afair.agents.prompts import MAX_USER_MESSAGE_CHARS, build_user_message
    from afair.mcp import handlers
    from afair.mcp.context import connect_for_thread
    from afair.substrate.events import read_event_by_id

    with _client(tmp_path):
        # Two large inline parts; combined they far exceed the message budget.
        big = "word " * 20_000
        result = handlers.remember(
            content=CompoundContent(
                type="compound",
                parts=[
                    CompoundTextPart(type="text", text=big, label="a"),
                    CompoundTextPart(type="text", text=big, label="b"),
                ],
            ),
        )
        db = connect_for_thread()
        event = read_event_by_id(db, result.event_id)
        assert event is not None
        msg = build_user_message(event)
        # The raw parts list must not leak in verbatim; the flattened text is
        # governed by the truncation budget (message stays bounded).
        assert '"parts"' not in msg
        assert len(msg) < MAX_USER_MESSAGE_CHARS + 5_000


# ── P2b: canonicalizer grounding reads compound parts ───────────────────────


def test_compound_grounding_text_reads_parts(tmp_path: Path) -> None:
    """The canonicalizer's grounding + surrounding helpers must see compound
    part text — otherwise every compound relation is dropped as
    edges_skipped_no_evidence."""
    from afair.agents.entity_canonicalizer import (
        _event_grounding_text,
        _event_surrounding_text,
    )
    from afair.mcp import handlers
    from afair.mcp.context import connect_for_thread
    from afair.substrate.events import read_event_by_id

    with _client(tmp_path):
        result = handlers.remember(
            content=CompoundContent(
                type="compound",
                parts=[
                    CompoundTextPart(type="text", text="Maya leads the platform team", label="q"),
                ],
            ),
        )
        db = connect_for_thread()
        event = read_event_by_id(db, result.event_id)
        assert event is not None
        # No extractor summary/extracted_text available → must fall to parts.
        grounding = _event_grounding_text(event, {})
        assert "Maya leads the platform team" in grounding
        surrounding = _event_surrounding_text(event, {})
        assert "Maya leads the platform team" in surrounding
