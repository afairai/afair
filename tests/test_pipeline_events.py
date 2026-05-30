"""pipeline_events lifecycle tracing tests.

Verifies the table is append-only, the helper writes the expected
row shapes, and the lifecycle instrumentation in remember/observe/
extract fires.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from afair.substrate import open_db
from afair.substrate import pipeline_events as pe

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path)
    try:
        yield conn
    finally:
        conn.close()


def test_record_writes_a_row(db: sqlite3.Connection) -> None:
    pe.record(
        db,
        event_id="01EVENT",
        event_hash="sha256:abc",
        stage=pe.STAGE_EVENT_WRITTEN,
        producer="remember",
    )
    row = db.execute("SELECT event_id, stage, status, producer FROM pipeline_events").fetchone()
    assert row["event_id"] == "01EVENT"
    assert row["stage"] == pe.STAGE_EVENT_WRITTEN
    assert row["status"] == pe.STATUS_OK
    assert row["producer"] == "remember"


def test_record_caps_detail_length(db: sqlite3.Connection) -> None:
    """Adversarially long detail strings get truncated to 500 chars."""
    long_detail = "x" * 2000
    pe.record(
        db,
        event_id="01EVENT",
        stage=pe.STAGE_EXTRACTION_FAILED,
        status=pe.STATUS_FAILED,
        detail=long_detail,
    )
    row = db.execute("SELECT detail FROM pipeline_events").fetchone()
    assert len(row["detail"]) <= 500


def test_pipeline_events_are_append_only(db: sqlite3.Connection) -> None:
    """The I2 triggers reject UPDATE + DELETE — tracing is immutable
    like the substrate it traces."""
    pe.record(db, event_id="01EVENT", stage=pe.STAGE_EVENT_WRITTEN)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE pipeline_events SET status = 'fake'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM pipeline_events")


def test_record_safe_swallows_failures() -> None:
    """When the factory yields nothing, record_safe must NOT raise.

    Tracing is advisory; a missing context should never break the
    calling code path."""
    pe.record_safe(
        lambda: None,
        event_id="01EVENT",
        stage=pe.STAGE_EVENT_WRITTEN,
    )


def test_lifecycle_remember_to_extract(tmp_path: Path) -> None:
    """End-to-end: remember() then synchronous extract should write
    at least event.written + extraction.enqueued + extraction.started +
    extraction.completed rows."""
    import json

    from afair.agents import extractor
    from afair.agents.llm import LLMResult
    from afair.mcp import handlers
    from afair.mcp.context import ServerContext, clear_context, set_context
    from afair.mcp.schemas import TextContent

    db_conn = open_db(tmp_path)
    set_context(
        ServerContext(
            db=db_conn,
            vault_dir=tmp_path,
            inline_text_max_bytes=64 * 1024,
            semantic_recall_enabled=False,  # skip embedding noise
        )
    )

    def fake_call_tool(**_: object) -> LLMResult:
        good = {"best_guess_kind": "note", "summary": "test note"}
        return LLMResult(data=good, model="mock", raw=json.dumps(good))

    try:
        import pytest as _pytest

        with _pytest.MonkeyPatch.context() as m:
            m.setattr("afair.agents.extractor.call_tool", fake_call_tool)
            m.setattr("afair.mcp.handlers.schedule_extraction", extractor.extract_sync)

            handlers.remember(
                content=TextContent(type="text", text="hello world"),
                context="ctx",
            )
        rows = db_conn.execute(
            "SELECT stage, status FROM pipeline_events ORDER BY recorded_at"
        ).fetchall()
        stages = [r["stage"] for r in rows]
        # The test patches schedule_extraction directly to extract_sync,
        # so the "enqueued" marker from the real schedule_extraction
        # doesn't fire. Verify the rest of the lifecycle.
        assert pe.STAGE_EVENT_WRITTEN in stages
        assert pe.STAGE_EXTRACTION_STARTED in stages
        assert pe.STAGE_EXTRACTION_COMPLETED in stages
        # All successful — no FAILED rows for the happy path.
        statuses = {r["status"] for r in rows}
        assert pe.STATUS_FAILED not in statuses
    finally:
        clear_context()
        db_conn.close()
