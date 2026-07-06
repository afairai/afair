"""pipeline_events lifecycle tracing tests.

Verifies the helper writes the expected row shapes, the table is prunable
telemetry (ADR-0005, not append-only memory), and the lifecycle
instrumentation in remember/observe/extract fires.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from afair.substrate import open_db
from afair.substrate import pipeline_events as pe

if TYPE_CHECKING:
    import sqlite3
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


def test_pipeline_events_are_prunable_telemetry(db: sqlite3.Connection) -> None:
    """ADR-0005: pipeline_events is OPERATIONAL TELEMETRY, not user memory —
    the append-only I2 triggers were retired so the Pruner can age it out.
    A DELETE (and UPDATE) must now SUCCEED, unlike the memory substrate."""
    pe.record(db, event_id="01EVENT", stage=pe.STAGE_EVENT_WRITTEN)
    # No append-only trigger exists on either telemetry table.
    triggers = db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' "
        "AND tbl_name IN ('pipeline_events', 'observability_snapshots')"
    ).fetchall()
    assert triggers == []
    # DELETE + UPDATE succeed (no ABORT).
    db.execute("UPDATE pipeline_events SET status = 'fake'")
    db.execute("DELETE FROM pipeline_events")
    db.commit()
    assert db.execute("SELECT COUNT(*) AS n FROM pipeline_events").fetchone()["n"] == 0


def test_legacy_vault_telemetry_triggers_retired_on_open(tmp_path: Path) -> None:
    """ADR-0005 migration: a vault created BEFORE the ADR still has the four
    append-only triggers on pipeline_events + observability_snapshots. Opening
    it (init runs SCHEMA_DDL, which now carries DROP TRIGGER IF EXISTS) must
    retire exactly those four, and a DELETE must then succeed. Idempotent on a
    second open. This pins the exact path the live vault runs next boot."""
    telemetry = ("pipeline_events", "observability_snapshots")

    conn = open_db(tmp_path)
    # Re-create the pre-ADR-0005 append-only triggers to simulate a legacy vault.
    for tbl in telemetry:
        conn.execute(
            f"CREATE TRIGGER {tbl}_no_update BEFORE UPDATE ON {tbl} "
            f"BEGIN SELECT RAISE(ABORT, '{tbl} is append-only (Invariant I2)'); END"
        )
        conn.execute(
            f"CREATE TRIGGER {tbl}_no_delete BEFORE DELETE ON {tbl} "
            f"BEGIN SELECT RAISE(ABORT, '{tbl} is append-only (Invariant I2)'); END"
        )
    conn.commit()
    assert _telemetry_trigger_count(conn) == 4
    conn.close()

    # Re-open = boot on the legacy vault → the DROP TRIGGER statements fire.
    conn = open_db(tmp_path)
    assert _telemetry_trigger_count(conn) == 0
    # And a DELETE now works (no ABORT).
    pe.record(conn, event_id="01EV", stage=pe.STAGE_EVENT_WRITTEN)
    conn.execute("DELETE FROM pipeline_events")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) AS n FROM pipeline_events").fetchone()["n"] == 0
    conn.close()

    # Idempotent: a second open is a clean no-op, still zero triggers.
    conn = open_db(tmp_path)
    assert _telemetry_trigger_count(conn) == 0
    conn.close()


def _telemetry_trigger_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'trigger' "
        "AND tbl_name IN ('pipeline_events', 'observability_snapshots')"
    ).fetchone()["n"]


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
