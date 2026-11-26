"""Durable gaps survive restart; duplicate and deferred work spends no retries."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from afair.agents import extractor, foreground
from afair.agents.extraction_recovery import (
    extraction_audit,
    recover_missing_extractions,
    repair_missing_vectors,
    select_missing_extractions,
)
from afair.agents.interpretation import write_interpretation
from afair.agents.llm import LLMResult
from afair.agents.usage import BackgroundBudgetExceeded
from afair.mcp import handlers
from afair.mcp.context import ServerContext, clear_context, set_context
from afair.settings import Settings
from afair.substrate import open_db, write_event

GOOD: dict[str, Any] = {
    "summary": "A project decision",
    "entities": [],
    "relations": [],
    "best_guess_kind": "decision",
    "time_references": [],
    "salient_facts": [],
}


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    db = open_db(tmp_path)
    monkeypatch.setattr("afair.substrate.watermarks._lag_threshold_id", lambda: "Z" * 26)
    settings = Settings(_env_file=None, environment="local", vault_dir=tmp_path)
    sc = ServerContext(
        db=db,
        vault_dir=tmp_path,
        inline_text_max_bytes=65536,
        semantic_recall_enabled=False,
        background_usage_settings=settings,
    )
    set_context(sc)
    monkeypatch.setattr(foreground, "_last_completed", float("-inf"))
    monkeypatch.setattr(foreground, "_active", 0)
    yield sc
    clear_context()
    db.close()


def event(ctx, n=1, **kwargs):
    return write_event(
        ctx.db,
        origin="agent",
        kind="remember",
        payload={"content_type": "text", "text": f"Project decision {n}", **kwargs},
        created_at="2020-01-01T00:00:00+00:00",
    )


def patch_llm(monkeypatch):
    calls = []

    def call(**kwargs):
        calls.append(kwargs)
        return LLMResult(data=GOOD, model="mock", raw="{}")

    monkeypatch.setattr(extractor, "call_tool", call)
    return calls


def test_missing_intake_recovered_after_restart_without_telemetry(ctx, monkeypatch):
    source = event(ctx)
    raw = tuple(ctx.db.execute("SELECT * FROM events WHERE id=?", (source.id,)).fetchone())
    calls = patch_llm(monkeypatch)
    # Simulate losing all process-local queue/context state after commit.
    clear_context()
    set_context(ctx)
    assert extraction_audit(ctx.db)["missing_extraction"] == 1
    assert recover_missing_extractions(ctx.db)["attempted"] == 1
    assert recover_missing_extractions(ctx.db)["attempted"] == 0
    assert len(calls) == 1
    assert tuple(ctx.db.execute("SELECT * FROM events WHERE id=?", (source.id,)).fetchone()) == raw
    assert extraction_audit(ctx.db)["successful_extraction"] == 1


def test_recovery_bounded_excludes_generated_and_recent(ctx, monkeypatch):
    for n in range(7):
        event(ctx, n)
    event(ctx, 20, produced_by="consolidator:v1")
    write_event(
        ctx.db,
        origin="agent:mode_switcher",
        kind="observe",
        payload={"content_type": "event", "action": "mode_switch"},
    )
    write_event(
        ctx.db,
        origin="agent",
        kind="remember",
        payload={"content_type": "text", "text": "Just now"},
    )
    calls = patch_llm(monkeypatch)
    assert recover_missing_extractions(ctx.db, limit=3)["attempted"] == 3
    assert len(select_missing_extractions(ctx.db)) == 4
    assert len(calls) == 3


def test_budget_refusal_stays_pending_without_failed_row(ctx, monkeypatch):
    event(ctx)

    def refuse(**kwargs):
        raise BackgroundBudgetExceeded("budget")

    monkeypatch.setattr(extractor, "call_tool", refuse)
    assert recover_missing_extractions(ctx.db)["deferred"] == 1
    assert ctx.db.execute("SELECT COUNT(*) FROM interpretations").fetchone()[0] == 0
    clear_context()
    set_context(ctx)
    calls = patch_llm(monkeypatch)
    # Simulate the durable five-minute backoff elapsing across restart.
    with ctx.db:
        ctx.db.execute("UPDATE extraction_jobs SET available_at=0")
    assert recover_missing_extractions(ctx.db)["attempted"] == 1
    assert len(calls) == 1
    assert not select_missing_extractions(ctx.db)


def test_stale_recovery_candidate_and_duplicate_schedule_do_not_reextract(ctx, monkeypatch):
    source = event(ctx)
    calls = patch_llm(monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(extractor.extract_sync, source.id, only_if_missing=True) for _ in range(2)
        ]
        for future in futures:
            future.result()
    assert len(calls) == 1
    assert ctx.db.execute("SELECT COUNT(*) FROM interpretations").fetchone()[0] == 1


def test_vector_repair_reuses_success_and_is_bounded_idempotent(ctx, monkeypatch):
    for n in range(3):
        source = event(ctx, n)
        write_interpretation(
            ctx.db, event=source, version=1, produced_by="extractor:mock", extraction=GOOD
        )
    calls = []

    def embed(**kwargs):
        calls.append(kwargs)
        return [0.1] * 1536

    monkeypatch.setattr("afair.agents.embedding.embed_text", embed)
    stats = repair_missing_vectors(ctx.db, ctx.background_usage_settings, limit=2)
    assert stats["repaired"] == 2
    assert repair_missing_vectors(ctx.db, ctx.background_usage_settings, limit=2)["repaired"] == 1
    assert repair_missing_vectors(ctx.db, ctx.background_usage_settings, limit=2)["repaired"] == 0
    assert len(calls) == 3
    assert ctx.db.execute("SELECT COUNT(*) FROM interpretations").fetchone()[0] == 3


def test_foreground_yields_and_releases_after_exception(ctx):
    event(ctx)

    @foreground.foreground_request
    def request():
        assert foreground.background_should_yield()
        assert recover_missing_extractions(ctx.db)["deferred"] == 1
        raise ValueError("failed recall")

    with pytest.raises(ValueError):
        request()
    assert foreground._active == 0
    assert foreground.background_should_yield()  # quiet period


def test_recall_bounded_wait_returns_local_results(ctx, monkeypatch):
    source = event(ctx)
    ctx.semantic_recall_enabled = True
    started = threading.Event()
    release = threading.Event()

    def slow(**kwargs):
        started.set()
        release.wait(timeout=5)
        return [0.1] * 1536

    monkeypatch.setattr(handlers, "embed_query", slow)
    monkeypatch.setattr(handlers, "SEMANTIC_WAIT_SECONDS", 0.01)
    try:
        result = handlers.recall(query="Project", depth="normal")
        assert started.is_set()
        assert source.id in [hit.event_id for hit in result.hits]
        assert "FTS-only" in result.note
        assert result.depth_used == "shallow"
    finally:
        release.set()


def test_recall_full_embedding_capacity_does_not_queue(ctx, monkeypatch):
    event(ctx)
    ctx.semantic_recall_enabled = True
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(handlers, "_RECALL_EMBEDDING_SLOTS", slots)

    def forbidden(**kwargs):
        pytest.fail("must not queue a provider call")

    monkeypatch.setattr(handlers, "embed_query", forbidden)
    result = handlers.recall(query="Project", depth="normal")
    assert result.hits
    assert "FTS-only" in result.note


@pytest.mark.parametrize(
    "query",
    [
        "aktuelle Rolle",
        "aktueller Arbeitgeber",
        "derzeit bei welcher Firma",
        "momentan zuständig",
        "neueste Entscheidung",
        "Was gilt jetzt?",
        "Was ist heute geplant?",
        "inzwischen geändert",
        "gegenwärtig tätig",
    ],
)
def test_german_temporal_intent(query):
    assert handlers._has_temporal_intent(query)


def test_recovery_discovery_is_bounded_and_idle_uses_cursor(ctx, monkeypatch):
    from afair.agents.extraction_recovery import discover_missing_extractions

    monkeypatch.setattr("afair.agents.extraction_recovery.MAX_DISCOVERY_ROWS", 3)
    for n in range(10):
        event(ctx, n)
    assert [discover_missing_extractions(ctx.db) for _ in range(5)] == [3, 3, 3, 1, 0]
    cursor = ctx.db.execute(
        "SELECT through_id FROM worker_watermarks WHERE worker='extraction_recovery_discovery'"
    ).fetchone()[0]
    plan = ctx.db.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM events WHERE id > ? AND id < ? ORDER BY id LIMIT 500",
        (cursor, "Z" * 26),
    ).fetchall()
    assert any("SEARCH" in row[3] and "id>" in row[3] for row in plan)
    assert ctx.db.execute("SELECT COUNT(*) FROM extraction_jobs").fetchone()[0] == 10


def test_budget_deferral_does_not_starve_later_candidates(ctx, monkeypatch):
    event(ctx, 1)
    event(ctx, 2)
    calls = []

    def call(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise BackgroundBudgetExceeded("unpriced first input")
        return LLMResult(data=GOOD, model="mock", raw="{}")

    monkeypatch.setattr(extractor, "call_tool", call)
    stats = recover_missing_extractions(ctx.db)
    assert stats["deferred"] == 1
    assert stats["attempted"] == 1
    assert len(calls) == 2
    assert extraction_audit(ctx.db)["successful_extraction"] == 1
    assert recover_missing_extractions(ctx.db)["attempted"] == 0


def test_lease_excludes_other_connections_and_recovers_expired_claim(ctx):
    from afair.agents.extraction_jobs import ExtractionDeferred, extraction_lease

    source = event(ctx)
    other = open_db(ctx.vault_dir)
    try:
        with (
            extraction_lease(ctx.db, source.id),
            pytest.raises(ExtractionDeferred),
            extraction_lease(other, source.id),
        ):
            pytest.fail("double claim")
        with ctx.db:
            ctx.db.execute("UPDATE extraction_jobs SET available_at=0,lease_until=0")
        with extraction_lease(other, source.id):
            pass
    finally:
        other.close()


def test_audit_connection_is_readonly_and_does_not_initialize_missing_vault(ctx, tmp_path):
    import sqlite3

    from afair.substrate.db import open_db_readonly

    event(ctx)
    conn = open_db_readonly(ctx.vault_dir)
    try:
        assert extraction_audit(conn)["eligible"] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE forbidden(value TEXT)")
    finally:
        conn.close()
    missing = tmp_path / "does-not-exist"
    with pytest.raises(sqlite3.OperationalError):
        open_db_readonly(missing)
    assert not missing.exists()


def test_recall_timing_has_no_content(ctx, monkeypatch):
    from afair.mcp import recall_timing

    source = event(ctx)
    records = []
    monkeypatch.setattr(
        recall_timing.log, "info", lambda name, **fields: records.append((name, fields))
    )
    handlers.recall(by_id=source.id)
    assert len(records) == 1
    name, fields = records[0]
    assert name == "recall.timing"
    assert set(fields) == {
        "total_ms",
        "embedding_wait_ms",
        "local_sqlite_hydration_ms",
        "semantic_fallback",
        "succeeded",
    }
    assert fields["succeeded"]
    assert fields["total_ms"] >= fields["embedding_wait_ms"]


def test_cross_process_lease_excludes_another_extractor(ctx):
    import subprocess
    import sys

    from afair.agents.extraction_jobs import extraction_lease

    source = event(ctx)
    code = """from pathlib import Path
import sys
from afair.substrate import open_db
from afair.agents.extraction_jobs import extraction_lease,ExtractionDeferred
conn=open_db(Path(sys.argv[1]))
try:
    with extraction_lease(conn,sys.argv[2]):
        print('provider would run')
except ExtractionDeferred:
    print('deferred')
finally: conn.close()
"""
    with extraction_lease(ctx.db, source.id):
        result = subprocess.run(
            [sys.executable, "-c", code, str(ctx.vault_dir), source.id],
            text=True,
            capture_output=True,
            check=True,
        )
    assert result.stdout.strip() == "deferred"


def test_semantic_deadline_does_not_restart_after_fts(ctx, monkeypatch):
    from concurrent.futures import Future, TimeoutError

    event(ctx)
    ctx.semantic_recall_enabled = True
    now = [0.0]
    monkeypatch.setattr(handlers.time, "monotonic", lambda: now[0])
    real_fts = handlers.search_fts

    def slow_fts(*args, **kwargs):
        now[0] += 3
        return real_fts(*args, **kwargs)

    class Pending(Future):
        def result(self, timeout=None):
            assert timeout == 0
            raise TimeoutError

    future = Pending()
    monkeypatch.setattr(handlers._RECALL_POOL, "submit", lambda *args, **kwargs: future)
    monkeypatch.setattr(handlers, "search_fts", slow_fts)
    result = handlers.recall(query="Project", depth="normal")
    assert result.hits
    assert result.depth_used == "shallow"


def test_warm_executor_queue_is_bounded_and_recoverable(ctx, monkeypatch):
    from concurrent.futures import Future

    first = event(ctx, 1)
    second = event(ctx, 2)
    submitted = []
    future = Future()

    def submit(*args, **kwargs):
        submitted.append(kwargs)
        return future

    monkeypatch.setattr(extractor._EXECUTOR, "submit", submit)
    monkeypatch.setattr(extractor, "_EXTRACTION_QUEUE_SLOTS", threading.BoundedSemaphore(1))
    extractor.schedule_extraction(first.id)
    extractor.schedule_extraction(second.id)
    assert len(submitted) == 1
    future.cancel()
    assert len(select_missing_extractions(ctx.db)) == 2


@pytest.mark.parametrize("refusal_mode", ["zero_cap", "concurrent_exhaustion"])
def test_audio_transcript_survives_budget_deferral_and_restart(ctx, monkeypatch, refusal_mode):
    from afair.substrate import write_object

    blob = write_object(ctx.vault_dir, b"audio source")
    source = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={
            "content_type": "binary",
            "blob_hash": blob,
            "mime": "audio/mpeg",
            "size_bytes": 12,
            "filename_hint": "memo.mp3",
        },
        created_at="2020-01-01T00:00:00+00:00",
    )
    transcripts = []

    def transcribe(**kwargs):
        transcripts.append(kwargs)
        return "Paid transcription survives downstream budget exhaustion"

    monkeypatch.setattr(extractor, "transcribe_audio", transcribe)

    def exhausted_after_audio(**kwargs):
        raise BackgroundBudgetExceeded("another worker reserved the remaining generation budget")

    if refusal_mode == "zero_cap":
        ctx.background_usage_settings = ctx.background_usage_settings.model_copy(
            update={"background_daily_budget_usd": 0}
        )
    else:
        monkeypatch.setattr(extractor, "call_tool", exhausted_after_audio)
    for _ in range(2):
        assert recover_missing_extractions(ctx.db)["deferred"] == 1
        assert ctx.db.execute("SELECT COUNT(*) FROM interpretations").fetchone()[0] == 0
        # Reopen the extraction connection to simulate a process restart and
        # expire its durable backoff before the next real recovery attempt.
        extractor.clear_extractor_thread_db()
        with ctx.db:
            ctx.db.execute("UPDATE extraction_jobs SET available_at=0,lease_until=0")
    ctx.background_usage_settings = ctx.background_usage_settings.model_copy(
        update={"background_daily_budget_usd": None}
    )
    calls = patch_llm(monkeypatch)
    assert recover_missing_extractions(ctx.db)["attempted"] == 1
    assert len(transcripts) == 1
    assert len(calls) == 1
    assert (
        ctx.db.execute(
            "SELECT transcript FROM extraction_transcripts WHERE event_id=?", (source.id,)
        )
        .fetchone()[0]
        .startswith("Paid transcription")
    )
    assert not select_missing_extractions(ctx.db)
