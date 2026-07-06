"""Every extraction-failure branch records a terminal pipeline stage (P0-3).

Regression target: the warm-path Extractor has seven distinct failure
branches, but only the validation branch used to append the
``extraction.failed`` pipeline row. The other six (PDF, audio, text-large,
image-payload, image-description, LLM error) returned after writing a
``status: failed`` interpretation WITHOUT a terminal pipeline stage, so
``pipeline_events.timeline()`` for those events ended at
``extraction.started``. The Phase 0.5 ExpectationChecker then miscounted
each as a silent-drop ``stuck_extraction`` for a full week.

The fix centralizes the terminal-stage record inside
``write_failed_interpretation`` (interpretation.py) so every branch that
routes a failure through it emits exactly one ``extraction.failed`` row —
and the previously-explicit record in the validation branch is removed so
it is not double-counted.

These tests drive each branch end-to-end (real SQLite, mocked
collaborator) and assert exactly one terminal row + a timeline that ends
at ``extraction.failed``. A separate test wires the traced failure into
the real ExpectationChecker to prove it is no longer counted stuck.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from ulid import ULID

from afair.agents import extractor
from afair.agents.binary_extractors import (
    AudioTranscriptionError,
    ImageDescriptionError,
    PdfExtractionError,
)
from afair.agents.expectation_checker import ExpectationChecker
from afair.agents.llm import LLMResult, LLMTimeout
from afair.mcp.context import ServerContext, clear_context, set_context
from afair.settings import Settings
from afair.substrate import open_db, write_event, write_object
from afair.substrate import pipeline_events as pe

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path

    from afair.substrate.events import Event


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[ServerContext]:
    db = open_db(tmp_path)
    sc = ServerContext(
        db=db,
        vault_dir=tmp_path,
        inline_text_max_bytes=64 * 1024,
        anthropic_api_key=None,
    )
    set_context(sc)
    try:
        yield sc
    finally:
        db.close()
        clear_context()


@pytest.fixture
def settings_local(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=tmp_path,
    )


# ── branch builders ──────────────────────────────────────────────────────────
#
# Each builder writes the event that routes to one failure branch and
# monkeypatches the collaborator that branch depends on so it fails
# deterministically. Returns the event.


def _build_pdf(ctx: ServerContext, monkeypatch: pytest.MonkeyPatch) -> Event:
    blob_hash = write_object(ctx.vault_dir, b"%PDF-1.4 not really a pdf")
    event = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={
            "content_type": "binary",
            "blob_hash": blob_hash,
            "mime": "application/pdf",
            "filename_hint": "broken.pdf",
        },
    )

    def _raise(*_: object, **__: object) -> str:
        raise PdfExtractionError("cannot parse pdf")

    monkeypatch.setattr("afair.agents.extractor.extract_pdf_text", _raise)
    return event


def _build_audio(ctx: ServerContext, monkeypatch: pytest.MonkeyPatch) -> Event:
    blob_hash = write_object(ctx.vault_dir, b"fake-audio-bytes")
    event = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={
            "content_type": "binary",
            "blob_hash": blob_hash,
            "mime": "audio/mpeg",
            "filename_hint": "voice.mp3",
        },
    )

    def _raise(*_: object, **__: object) -> str:
        raise AudioTranscriptionError("whisper down")

    monkeypatch.setattr("afair.agents.extractor.transcribe_audio", _raise)
    return event


def _build_text_large(ctx: ServerContext, monkeypatch: pytest.MonkeyPatch) -> Event:
    # A text-large event carries only a blob_hash; the extractor rehydrates
    # the body from the object store. Make that read fail.
    event = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={
            "content_type": "text-large",
            "blob_hash": "sha256:" + "0" * 64,
            "context": None,
        },
    )

    def _raise(*_: object, **__: object) -> bytes:
        raise OSError("blob unreadable")

    monkeypatch.setattr("afair.agents.extractor.read_object", _raise)
    return event


def _build_image_payload(ctx: ServerContext, _monkeypatch: pytest.MonkeyPatch) -> Event:
    # Image modality but the payload is missing its blob_hash → the
    # image-payload branch fires without any collaborator call.
    return write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={
            "content_type": "binary",
            "mime": "image/png",
            "filename_hint": "screenshot.png",
        },
    )


def _build_image_description(ctx: ServerContext, monkeypatch: pytest.MonkeyPatch) -> Event:
    blob_hash = write_object(ctx.vault_dir, b"PNG fake bytes")
    event = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={
            "content_type": "binary",
            "blob_hash": blob_hash,
            "mime": "image/png",
            "filename_hint": "screenshot.png",
        },
    )

    def _raise(*_: object, **__: object) -> dict[str, Any]:
        raise ImageDescriptionError("vision model refused")

    monkeypatch.setattr("afair.agents.extractor.describe_image", _raise)
    return event


def _build_llm_error(ctx: ServerContext, monkeypatch: pytest.MonkeyPatch) -> Event:
    event = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "a normal memory that times out"},
    )

    def _raise(**_: object) -> LLMResult:
        raise LLMTimeout("upstream timed out")

    monkeypatch.setattr("afair.agents.extractor.call_tool", _raise)
    return event


_BRANCHES = {
    "pdf": _build_pdf,
    "audio": _build_audio,
    "text_large": _build_text_large,
    "image_payload": _build_image_payload,
    "image_description": _build_image_description,
    "llm_error": _build_llm_error,
}


# ── helpers ──────────────────────────────────────────────────────────────────


def _failed_stage_rows(ctx: ServerContext, event_id: str) -> list[sqlite3.Row]:
    return ctx.db.execute(
        "SELECT * FROM pipeline_events WHERE event_id = ? AND stage = ?",
        (event_id, pe.STAGE_EXTRACTION_FAILED),
    ).fetchall()


# ── one terminal row per failure branch ──────────────────────────────────────


@pytest.mark.parametrize("branch", list(_BRANCHES))
def test_every_failure_branch_records_extraction_failed(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch, branch: str
) -> None:
    """Each of the six previously-untraced branches now ends the pipeline at
    ``extraction.failed`` with exactly one terminal row."""
    event = _BRANCHES[branch](ctx, monkeypatch)

    extractor.extract_sync(event.id)

    failed = _failed_stage_rows(ctx, event.id)
    assert len(failed) == 1, f"{branch}: expected exactly one extraction.failed row"
    assert failed[0]["status"] == pe.STATUS_FAILED

    timeline = pe.timeline(ctx.db, event.id)
    assert timeline, f"{branch}: no pipeline rows recorded at all"
    assert timeline[-1]["stage"] == pe.STAGE_EXTRACTION_FAILED

    # The failed interpretation row is still the durable record (I2/I3).
    interp = ctx.db.execute(
        "SELECT extraction FROM interpretations WHERE event_id = ? "
        "AND produced_by LIKE 'extractor:%'",
        (event.id,),
    ).fetchone()
    assert interp is not None


def test_validation_branch_records_exactly_once(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The validation branch used to record the terminal stage explicitly AND
    (after centralization) through write_failed_interpretation. The explicit
    record was removed, so it must land exactly once — not twice."""
    event = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "an event with a broken extraction"},
    )
    # A response missing the mandatory ``summary`` key → validation failure.
    bad = {"best_guess_kind": "note"}

    def _fake_call(**_: object) -> LLMResult:
        return LLMResult(data=bad, model="mock", raw="{}")

    monkeypatch.setattr("afair.agents.extractor.call_tool", _fake_call)

    extractor.extract_sync(event.id)

    failed = _failed_stage_rows(ctx, event.id)
    assert len(failed) == 1  # double-record guard
    timeline = pe.timeline(ctx.db, event.id)
    assert timeline[-1]["stage"] == pe.STAGE_EXTRACTION_FAILED


# ── the checker no longer miscounts a traced failure ─────────────────────────


def _insert_backdated_written(ctx: ServerContext, event: Event, *, hours: float) -> None:
    """Raw event.written row with a backdated recorded_at (past the grace
    window) so the checker's stuck query has an anchor to evaluate."""
    recorded_at = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    with ctx.db:
        ctx.db.execute(
            """
            INSERT INTO pipeline_events
                (id, event_id, event_hash, stage, status, recorded_at, producer, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(ULID()),
                event.id,
                event.content_hash,
                pe.STAGE_EVENT_WRITTEN,
                pe.STATUS_OK,
                recorded_at,
                "test",
                None,
            ),
        )


def test_traced_failure_not_counted_stuck_by_checker(
    ctx: ServerContext, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deterministic PDF failure that is now traced with extraction.failed
    is NOT a silent drop: the ExpectationChecker sees stuck_extractions == 0
    and counts it as a permanent (operator-attention) failure instead.

    Before the fix the PDF branch left no terminal stage, so an event.written
    older than the grace window would have been counted stuck."""
    event = _build_pdf(ctx, monkeypatch)
    _insert_backdated_written(ctx, event, hours=2)

    extractor.extract_sync(event.id)

    stats = ExpectationChecker().run(ctx.db, settings_local)
    assert stats["stuck_extractions"] == 0
    assert stats["permanent_failures"] == 1
    assert stats["expectation_violations"] == 0
