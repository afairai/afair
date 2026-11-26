"""Independent-review regressions for durable extraction scheduling."""

from __future__ import annotations

import sqlite3
import time
from concurrent.futures import Future
from pathlib import Path

import pytest

from afair.agents import extractor
from afair.agents.extraction_jobs import (
    MAX_JOB_FAILURES,
    extraction_lease,
)
from afair.agents.extraction_recovery import (
    discover_missing_extractions,
    recover_missing_extractions,
)
from afair.agents.extraction_retry import ExtractionRetryWorker
from afair.agents.interpretation import write_failed_interpretation
from afair.agents.llm import LLMResult
from afair.agents.usage import BackgroundBudgetExceeded
from afair.mcp import handlers
from afair.mcp.context import ServerContext, clear_context, set_context
from afair.mcp.schemas import TextContent
from afair.settings import Settings
from afair.substrate import open_db, write_event
from afair.substrate.db import _open_connection

GOOD = {"summary": "A durable decision", "entities": [], "relations": [], "best_guess_kind": "note"}


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    conn = open_db(tmp_path)
    settings = Settings(_env_file=None, environment="local", vault_dir=tmp_path)
    context = ServerContext(
        db=conn,
        vault_dir=tmp_path,
        inline_text_max_bytes=65536,
        semantic_recall_enabled=False,
        background_usage_settings=settings,
    )
    set_context(context)
    monkeypatch.setattr("afair.substrate.watermarks._lag_threshold_id", lambda: "Z" * 26)
    yield context
    clear_context()
    conn.close()


def source(ctx, text="A durable decision", **kwargs):
    return write_event(
        ctx.db,
        origin="agent",
        kind="remember",
        payload={"content_type": "text", "text": text, "context": None, "type_hint": None},
        created_at=kwargs.get("created_at", "2020-01-01T00:00:00+00:00"),
    )


def expire_backoff(conn, event_id):
    with conn:
        conn.execute("UPDATE extraction_jobs SET available_at=0 WHERE event_id=?", (event_id,))


def test_unexpected_after_provider_failures_have_finite_persistent_cap(ctx, monkeypatch):
    event = source(ctx)
    calls = []

    def provider(**kwargs):
        calls.append(kwargs)
        return LLMResult(data=GOOD, model="mock", raw="{}")

    def broken_write(*args, **kwargs):
        raise OSError("local interpretation write failed")

    monkeypatch.setattr(extractor, "call_tool", provider)
    monkeypatch.setattr(extractor, "write_interpretation", broken_write)
    for attempt in range(MAX_JOB_FAILURES):
        assert recover_missing_extractions(ctx.db)["errors"] == 1
        job = ctx.db.execute(
            "SELECT * FROM extraction_jobs WHERE event_id=?", (event.id,)
        ).fetchone()
        assert job["failures"] == attempt + 1
        assert job["available_at"] > time.time()
        assert recover_missing_extractions(ctx.db)["attempted"] == 0
        expire_backoff(ctx.db, event.id)
    # Neither restart nor the expiry of its last backoff resets the finite cap.
    clear_context()
    set_context(ctx)
    assert recover_missing_extractions(ctx.db)["attempted"] == 0
    assert len(calls) == MAX_JOB_FAILURES
    assert ctx.db.execute("SELECT terminal FROM extraction_jobs").fetchone()[0] == 1


def test_budget_deferrals_do_not_consume_operational_failures(ctx):
    event = source(ctx)
    for _ in range(MAX_JOB_FAILURES + 1):
        with pytest.raises(BackgroundBudgetExceeded), extraction_lease(ctx.db, event.id):
            raise BackgroundBudgetExceeded("allowance")
        expire_backoff(ctx.db, event.id)
    row = ctx.db.execute("SELECT failures,terminal FROM extraction_jobs").fetchone()
    assert tuple(row) == (0, 0)


def test_overflow_persists_recent_job_without_historical_sweep(ctx, monkeypatch):
    import threading

    source(ctx, "old history")
    discover_missing_extractions(ctx.db)
    recent = source(ctx, "recent overflow", created_at="2999-01-01T00:00:00+00:00")
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(extractor, "_EXTRACTION_QUEUE_SLOTS", slots)
    extractor.schedule_extraction(recent.id)
    row = ctx.db.execute("SELECT * FROM extraction_jobs WHERE event_id=?", (recent.id,)).fetchone()
    assert row is not None and row["purpose"] == "initial"
    # Simulate grace elapsed. No discovery of old history is required.
    expire_backoff(ctx.db, recent.id)
    monkeypatch.setattr(
        "afair.agents.extraction_recovery.discover_missing_extractions", lambda conn: 0
    )
    monkeypatch.setattr(
        extractor, "call_tool", lambda **kwargs: LLMResult(data=GOOD, model="mock", raw="{}")
    )
    assert recover_missing_extractions(ctx.db, limit=1)["attempted"] == 1
    assert ctx.db.execute("SELECT event_id FROM interpretations").fetchone()[0] == recent.id


def test_warm_failure_is_logged_without_exception_content(ctx, monkeypatch):
    event = source(ctx)
    future = Future()
    monkeypatch.setattr(extractor._EXECUTOR, "submit", lambda *args, **kwargs: future)
    logs = []
    monkeypatch.setattr(
        extractor.log, "warning", lambda name, **fields: logs.append((name, fields))
    )
    extractor.schedule_extraction(event.id)
    future.set_exception(OSError("private source bytes must not be logged"))
    assert logs == [
        ("extractor.background_failed", {"event_id": event.id, "error_type": "OSError"})
    ]


def test_discovery_handles_malformed_and_naive_legacy_dates(ctx):
    first = source(ctx, "malformed", created_at="definitely-not-a-date")
    second = source(ctx, "naive", created_at="2020-01-01T00:00:00")
    assert discover_missing_extractions(ctx.db) == 2
    rows = {
        row["event_id"]: row["available_at"]
        for row in ctx.db.execute("SELECT * FROM extraction_jobs")
    }
    assert rows[first.id] > time.time()
    assert rows[second.id] < time.time()


def test_authentic_agent_intake_survives_without_provenance_but_internal_events_do_not(
    ctx, monkeypatch
):
    monkeypatch.setattr(handlers, "schedule_extraction", lambda event_id: None)
    result = handlers.remember(TextContent(type="text", text="Authentic old intake"))
    # No credential-derived provenance exists in a historical/local invocation.
    assert ctx.db.execute("SELECT COUNT(*) FROM event_provenance").fetchone()[0] == 0
    generated = write_event(
        ctx.db,
        origin="agent",
        kind="consolidation",
        payload={"content_type": "text", "text": "generated", "produced_by": "consolidator:v0"},
    )
    audit = write_event(
        ctx.db,
        origin="user",
        kind="observe",
        payload={"content_type": "event", "action": "resolve_conflict", "subject": "cfl_x"},
    )
    discover_missing_extractions(ctx.db)
    jobs = {row[0] for row in ctx.db.execute("SELECT event_id FROM extraction_jobs")}
    assert result.event_id in jobs
    assert generated.id not in jobs
    assert audit.id not in jobs


@pytest.mark.parametrize("purpose", ["retry", "vector"])
@pytest.mark.parametrize("state", ["deterministic", "exhausted", "terminal_job"])
def test_stale_terminal_jobs_do_not_freeze_retry_watermark(ctx, monkeypatch, purpose, state):
    event = source(ctx)
    for _ in range(3 if state == "exhausted" else 1):
        write_failed_interpretation(
            ctx.db,
            event=event,
            version=1,
            produced_by="extractor:mock",
            error_type="llm_response_error" if state == "deterministic" else "llm_timeout",
            error_message="failure",
        )
    with ctx.db:
        ctx.db.execute(
            "INSERT INTO extraction_jobs(event_id,purpose,terminal,available_at) VALUES (?,?,?,?)",
            (event.id, purpose, int(state == "terminal_job"), time.time() + 10000),
        )
    frontier = ("2020-01-01", "Z" * 26)
    monkeypatch.setattr(
        "afair.substrate.watermarks.frontier_interpretations", lambda conn: frontier
    )
    ExtractionRetryWorker().run(ctx.db, ctx.background_usage_settings)
    assert (
        ctx.db.execute(
            "SELECT through_id FROM worker_watermarks WHERE worker='extraction_retry'"
        ).fetchone()[0]
        == frontier[1]
    )


def test_lease_cleanup_failure_preserves_primary_budget_error(ctx):
    class FailingRelease:
        def __init__(self, real):
            self.real = real
            self.fail = False

        def __getattr__(self, name):
            return getattr(self.real, name)

        def __enter__(self):
            self.real.__enter__()
            return self

        def __exit__(self, *args):
            return self.real.__exit__(*args)

        def execute(self, sql, *args):
            if self.fail:
                raise sqlite3.OperationalError("cleanup storage failure")
            return self.real.execute(sql, *args)

    conn = FailingRelease(ctx.db)
    event = source(ctx)
    with (
        pytest.raises(BackgroundBudgetExceeded, match="original refusal"),
        extraction_lease(conn, event.id),
    ):
        conn.fail = True
        raise BackgroundBudgetExceeded("original refusal")


def test_readonly_connection_accepts_relative_path(ctx, monkeypatch):
    monkeypatch.chdir(ctx.vault_dir)
    conn = _open_connection(Path("substrate.db"), vault_key=None, read_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE forbidden(x)")
    finally:
        conn.close()


@pytest.mark.parametrize("backend", ["sqlite", "sqlcipher"])
@pytest.mark.parametrize("verb", ["remember", "observe"])
def test_enqueue_busy_does_not_fail_committed_intake_or_skip_provenance(
    ctx, monkeypatch, backend, verb
):
    import sys
    from types import SimpleNamespace

    from afair.mcp.schemas import ObserveEvent

    class CipherError(Exception):
        pass

    class CipherBusy(CipherError):
        pass

    error_type = sqlite3.OperationalError
    if backend == "sqlcipher":
        # SQLCipher's separate hierarchy must be caught even in plaintext CI.
        monkeypatch.setitem(sys.modules, "sqlcipher3", SimpleNamespace(Error=CipherError))
        error_type = CipherBusy

    def busy(*args, **kwargs):
        raise error_type("database is locked")

    monkeypatch.setattr("afair.agents.extraction_jobs.enqueue_initial", busy)
    monkeypatch.setattr(handlers, "current_client", lambda: ("review-client", "bearer"))
    future = Future()
    monkeypatch.setattr(extractor._EXECUTOR, "submit", lambda *args, **kwargs: future)
    try:
        if verb == "remember":
            result = handlers.remember(
                TextContent(type="text", text="Save despite queue contention")
            )
        else:
            result = handlers.observe(ObserveEvent(action="read_file", subject="A local document"))
        assert result.ok
        assert (
            ctx.db.execute("SELECT COUNT(*) FROM events WHERE id=?", (result.event_id,)).fetchone()[
                0
            ]
            == 1
        )
        row = ctx.db.execute(
            "SELECT client,verb FROM event_provenance WHERE event_id=?", (result.event_id,)
        ).fetchone()
        assert tuple(row) == ("review-client", verb)
    finally:
        future.cancel()


def test_historical_content_audits_are_excluded_but_real_observations_and_corrections_survive(
    ctx, monkeypatch
):
    from afair.mcp.schemas import ObserveEvent

    monkeypatch.setattr(handlers, "schedule_extraction", lambda event_id: None)
    observation = handlers.observe(ObserveEvent(action="read_file", subject="User-owned document"))
    correction = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={
            "content_type": "text",
            "text": "A corrected durable fact",
            "type_hint": "correction",
        },
    )
    audit_ids = []
    for action in ("resolve_conflict", "correct_content", "suppress_key_point"):
        # Historical rows may predate a produced_by sidecar marker.
        audit_ids.append(
            write_event(
                ctx.db,
                origin="user",
                kind="observe",
                payload={"content_type": "event", "action": action, "subject": "historical audit"},
            ).id
        )
    discover_missing_extractions(ctx.db)
    jobs = {row[0] for row in ctx.db.execute("SELECT event_id FROM extraction_jobs")}
    assert observation.event_id in jobs
    assert correction.id in jobs
    assert not jobs.intersection(audit_ids)
