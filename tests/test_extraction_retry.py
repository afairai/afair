"""Extraction-retry worker tests — mocked LLM, no live network calls.

Covers the bounded cold-path retry for TRANSIENT extraction failures
(llm_timeout / llm_rate_limit). Deterministic failures must never be
re-selected; retries stop at MAX_EXTRACTION_RETRIES total failed
attempts; a successful retry appends a new interpretation row that
supersedes the failure (I2 append-only — the failed row stays as audit).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from afair.agents import extractor
from afair.agents.extraction_retry import (
    MAX_EXTRACTION_RETRIES,
    TRANSIENT_ERROR_TYPES,
    ExtractionRetryWorker,
    select_retry_candidates,
)
from afair.agents.interpretation import (
    read_latest_interpretation,
    write_failed_interpretation,
)
from afair.agents.llm import LLMResult, LLMTimeout
from afair.agents.prompts import EXTRACTOR_SCHEMA_VERSION
from afair.mcp.context import ServerContext, clear_context, set_context
from afair.settings import Settings
from afair.substrate import open_db, write_event

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from afair.substrate.events import Event

GOOD_EXTRACTION: dict[str, Any] = {
    "best_guess_kind": "note",
    "summary": "A large session-handoff document about helios.",
    "entities": [{"name": "helios", "type": "project"}],
    "relations": [],
    "time_references": [],
    "salient_facts": ["session handoff"],
    "language": "en",
    "confidence": 0.9,
    "source_attribution": "user",
}

PRODUCED_BY = "extractor:anthropic/claude-haiku-4-5"


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[ServerContext]:
    db = open_db(tmp_path)
    sc = ServerContext(
        db=db,
        vault_dir=tmp_path,
        inline_text_max_bytes=64 * 1024,
        # No real API keys in unit tests — call_tool is mocked.
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


# ── helpers ─────────────────────────────────────────────────────────────────


def _patch_llm(monkeypatch: pytest.MonkeyPatch, response: dict[str, Any]) -> None:
    """Replace the LLM call with one that returns a fixed dict."""

    def fake_call(**_: object) -> LLMResult:
        return LLMResult(data=response, model="mock", raw=json.dumps(response))

    monkeypatch.setattr("afair.agents.extractor.call_tool", fake_call)


def _patch_llm_raises(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    """Replace the LLM call with one that raises ``exc``."""

    def fake_call(**_: object) -> LLMResult:
        raise exc

    monkeypatch.setattr("afair.agents.extractor.call_tool", fake_call)


def _write_text_event(ctx: ServerContext, text: str) -> Event:
    return write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": text, "context": None, "type_hint": None},
    )


def _seed_failure(ctx: ServerContext, event: Event, error_type: str) -> None:
    write_failed_interpretation(
        ctx.db,
        event=event,
        version=EXTRACTOR_SCHEMA_VERSION,
        produced_by=PRODUCED_BY,
        error_type=error_type,
        error_message=f"seeded {error_type}",
    )


def _extractor_rows(ctx: ServerContext, event_hash: str) -> list[dict[str, Any]]:
    rows = ctx.db.execute(
        """
        SELECT extraction FROM interpretations
        WHERE event_hash = ? AND produced_by LIKE 'extractor:%'
        ORDER BY produced_at ASC
        """,
        (event_hash,),
    ).fetchall()
    return [json.loads(r["extraction"]) for r in rows]


# ── the retry itself ────────────────────────────────────────────────────────


def test_transient_failure_is_retried(
    ctx: ServerContext, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirror of the live-vault gap: an event whose latest (only) extractor
    interpretation is an llm_timeout failure with retries 0 must be picked
    up by the worker and re-extracted to success on the next cycle."""
    event = _write_text_event(ctx, "a large helios session-handoff document")
    _seed_failure(ctx, event, "llm_timeout")
    assert read_latest_interpretation(ctx.db, event.content_hash) is None  # gap is real

    # The work-selection must find exactly this event.
    assert select_retry_candidates(ctx.db) == [(event.id, event.content_hash)]

    _patch_llm(monkeypatch, GOOD_EXTRACTION)
    stats = ExtractionRetryWorker().run(ctx.db, settings_local)

    assert stats["candidates"] == 1
    assert stats["succeeded"] == 1
    latest = read_latest_interpretation(ctx.db, event.content_hash)
    assert latest is not None
    assert latest.extraction["status"] == "success"
    assert latest.extraction["summary"] == GOOD_EXTRACTION["summary"]
    # I2 append-only: the failed row is still there as audit trail.
    rows = _extractor_rows(ctx, event.content_hash)
    assert [r["status"] for r in rows] == ["failed", "success"]
    # Once succeeded, the event is no longer a retry candidate.
    assert select_retry_candidates(ctx.db) == []


def test_rate_limit_failure_is_also_transient(
    ctx: ServerContext, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = _write_text_event(ctx, "rate-limited on first attempt")
    _seed_failure(ctx, event, "llm_rate_limit")

    _patch_llm(monkeypatch, GOOD_EXTRACTION)
    stats = ExtractionRetryWorker().run(ctx.db, settings_local)

    assert stats["succeeded"] == 1
    latest = read_latest_interpretation(ctx.db, event.content_hash)
    assert latest is not None
    assert latest.extraction["status"] == "success"


def test_retry_capped(
    ctx: ServerContext, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An event that already failed MAX_EXTRACTION_RETRIES times is NOT
    re-selected — no infinite retry loop, even against a now-healthy LLM."""
    event = _write_text_event(ctx, "keeps timing out")
    for _ in range(MAX_EXTRACTION_RETRIES):
        _seed_failure(ctx, event, "llm_timeout")

    assert select_retry_candidates(ctx.db) == []

    _patch_llm(monkeypatch, GOOD_EXTRACTION)
    stats = ExtractionRetryWorker().run(ctx.db, settings_local)

    assert stats["candidates"] == 0
    assert stats["succeeded"] == 0
    assert read_latest_interpretation(ctx.db, event.content_hash) is None


def test_retry_failure_appends_row_and_counts_attempts_honestly(
    ctx: ServerContext, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry that fails again appends a NEW failed row (I2) whose
    ``retries`` field carries the honest prior-attempt count, and the
    worker keeps re-selecting the event until the cap, then stops."""
    event = _write_text_event(ctx, "still timing out")
    _seed_failure(ctx, event, "llm_timeout")
    _patch_llm_raises(monkeypatch, LLMTimeout("upstream timed out again"))

    worker = ExtractionRetryWorker()
    for expected_rows in range(2, MAX_EXTRACTION_RETRIES + 1):
        stats = worker.run(ctx.db, settings_local)
        assert stats["candidates"] == 1
        assert stats["still_failing"] == 1
        rows = _extractor_rows(ctx, event.content_hash)
        assert len(rows) == expected_rows
        assert all(r["status"] == "failed" for r in rows)
        # Honest attempt tracking: the newest row records prior failures.
        assert rows[-1]["retries"] == expected_rows - 1

    # Cap reached — the next cycle selects nothing.
    stats = worker.run(ctx.db, settings_local)
    assert stats["candidates"] == 0
    assert len(_extractor_rows(ctx, event.content_hash)) == MAX_EXTRACTION_RETRIES


def test_deterministic_failure_not_retried(
    ctx: ServerContext, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deterministic errors would just fail again — never re-selected."""
    for error_type in (
        "image_payload_error",
        "pdf_extraction_error",
        "text_large_read_error",
        "llm_response_error",
        "llm_auth_error",
    ):
        assert error_type not in TRANSIENT_ERROR_TYPES
        event = _write_text_event(ctx, f"deterministic failure {error_type}")
        _seed_failure(ctx, event, error_type)

    assert select_retry_candidates(ctx.db) == []

    _patch_llm(monkeypatch, GOOD_EXTRACTION)
    stats = ExtractionRetryWorker().run(ctx.db, settings_local)
    assert stats["candidates"] == 0
    assert stats["succeeded"] == 0


def test_never_attempted_still_extracted(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control — a fresh event with no interpretation still goes
    through the (unchanged) warm extraction path and succeeds."""
    _patch_llm(monkeypatch, GOOD_EXTRACTION)
    event = _write_text_event(ctx, "a brand-new memory")

    # Never-attempted events are NOT the retry worker's job (agent-written
    # events like consolidations intentionally have no extraction).
    assert select_retry_candidates(ctx.db) == []

    extractor.extract_sync(event.id)
    latest = read_latest_interpretation(ctx.db, event.content_hash)
    assert latest is not None
    assert latest.extraction["status"] == "success"


def test_successful_event_never_reselected(
    ctx: ServerContext, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An event whose LATEST interpretation is a success is left alone even
    though an older transient failure exists in its history."""
    event = _write_text_event(ctx, "failed once, then succeeded")
    _seed_failure(ctx, event, "llm_timeout")
    _patch_llm(monkeypatch, GOOD_EXTRACTION)
    extractor.extract_sync(event.id)
    latest = read_latest_interpretation(ctx.db, event.content_hash)
    assert latest is not None and latest.extraction["status"] == "success"

    assert select_retry_candidates(ctx.db) == []
    stats = ExtractionRetryWorker().run(ctx.db, settings_local)
    assert stats["candidates"] == 0


def test_per_run_bound_is_enforced(
    ctx: ServerContext, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One cycle never retries more than the per-run cap; the remainder is
    picked up on subsequent cycles."""
    from afair.agents.extraction_retry import MAX_RETRIES_PER_RUN

    n_events = MAX_RETRIES_PER_RUN + 2
    for i in range(n_events):
        event = _write_text_event(ctx, f"timed-out event {i}")
        _seed_failure(ctx, event, "llm_timeout")

    _patch_llm(monkeypatch, GOOD_EXTRACTION)
    worker = ExtractionRetryWorker()
    stats = worker.run(ctx.db, settings_local)
    assert stats["candidates"] == MAX_RETRIES_PER_RUN
    assert stats["succeeded"] == MAX_RETRIES_PER_RUN

    stats = worker.run(ctx.db, settings_local)
    assert stats["candidates"] == 2
    assert stats["succeeded"] == 2
    assert select_retry_candidates(ctx.db) == []


def test_worker_registered_in_server_lineup() -> None:
    """The worker must actually run on the live vault — assert it is part
    of the server's cold-path worker registration."""
    import inspect

    from afair.mcp import server

    source = inspect.getsource(server)
    assert "ExtractionRetryWorker()" in source
