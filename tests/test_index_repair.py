"""Synthetic repair regressions: no generation, exact association, repeat safety."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import pytest
from pydantic import ValidationError

from afair.agents import embedding, extractor, index_repair
from afair.agents.interpretation import write_interpretation
from afair.agents.usage import _SCOPE, BackgroundBudgetExceeded
from afair.settings import Settings
from afair.substrate import open_db, write_event
from afair.substrate import pipeline_events as pe
from afair.substrate.db import open_db_readonly


@pytest.fixture
def case(tmp_path, monkeypatch):
    conn = open_db(tmp_path)
    settings = Settings(_env_file=None, environment="local", vault_dir=tmp_path)
    event = write_event(
        conn,
        origin="agent",
        kind="remember",
        payload={"content_type": "text", "text": "Synthetic original sentinel"},
    )
    result = {
        "status": "success",
        "summary": "Synthetic derived sentinel",
        "best_guess_kind": "fact",
        "salient_facts": ["Synthetic fact"],
    }
    write_interpretation(
        conn, event=event, version=1, produced_by="extractor:synthetic", extraction=result
    )

    def forbidden(*args, **kwargs):
        pytest.fail("repair invoked extraction/generation")

    monkeypatch.setattr(extractor, "call_tool", forbidden)
    monkeypatch.setattr(extractor, "extract_sync", forbidden)
    yield conn, settings, event, result
    conn.close()


def test_audit_is_readonly_and_no_provider_or_job(case, monkeypatch):
    conn, settings, event, _ = case
    monkeypatch.setattr(embedding, "embed_text", lambda **kw: pytest.fail("audit provider"))
    before = conn.total_changes
    ro = open_db_readonly(settings.vault_dir)
    try:
        assert index_repair.audit_indexes(ro, [event.id])["fts_missing_or_different"] == 1
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("DELETE FROM extraction_jobs")
    finally:
        ro.close()
    assert conn.total_changes == before
    assert conn.execute("SELECT COUNT(*) FROM extraction_jobs").fetchone()[0] == 0


def test_fts_only_repair_is_exact_idempotent_and_preserves_substrate(case, monkeypatch):
    conn, settings, event, result = case
    other = write_event(
        conn,
        origin="agent",
        kind="remember",
        payload={"content_type": "text", "text": "Unrelated sentinel"},
    )
    before_events = [tuple(row) for row in conn.execute("SELECT * FROM events ORDER BY id")]
    before_interps = [
        tuple(row) for row in conn.execute("SELECT * FROM interpretations ORDER BY id")
    ]
    other_fts = conn.execute(
        "SELECT searchable_text FROM events_fts WHERE content_hash=?", (other.content_hash,)
    ).fetchone()[0]
    monkeypatch.setattr(embedding, "embed_text", lambda **kw: pytest.fail("FTS provider"))
    assert index_repair.repair_indexes(conn, settings, [event.id])["verified"] == 1
    assert conn.execute(
        "SELECT searchable_text FROM events_fts WHERE content_hash=?", (event.content_hash,)
    ).fetchone()[0] == extractor._extraction_searchable_text(event.payload, result, None)
    assert index_repair.repair_indexes(conn, settings, [event.id])["skipped"] == 1
    assert [tuple(r) for r in conn.execute("SELECT * FROM events ORDER BY id")] == before_events
    assert [
        tuple(r) for r in conn.execute("SELECT * FROM interpretations ORDER BY id")
    ] == before_interps
    assert (
        conn.execute(
            "SELECT searchable_text FROM events_fts WHERE content_hash=?", (other.content_hash,)
        ).fetchone()[0]
        == other_fts
    )
    traces = conn.execute(
        "SELECT * FROM pipeline_events WHERE producer='index_repair:v1'"
    ).fetchall()
    assert len(traces) == 1 and traces[0]["event_id"] == event.id
    assert traces[0]["event_hash"] == event.content_hash
    assert traces[0]["stage"] == pe.STAGE_EXTRACTION_COMPLETED


def test_optional_embedding_is_metered_and_never_overwrites(case, monkeypatch):
    conn, settings, event, _ = case
    calls = []

    def embed(**kwargs):
        assert _SCOPE.get() == (settings, "index_repair")
        calls.append(kwargs)
        return [0.1] * settings.embedding_dim

    monkeypatch.setattr(embedding, "embed_text", embed)
    assert index_repair.repair_indexes(conn, settings, [event.id], embeddings=True)["verified"] == 1
    assert len(calls) == 1
    assert "Synthetic derived sentinel" in calls[0]["text"]
    assert conn.execute("SELECT content_hash FROM events_vec").fetchone()[0] == event.content_hash
    assert index_repair.repair_indexes(conn, settings, [event.id], embeddings=True)["skipped"] == 1
    assert len(calls) == 1


@pytest.mark.parametrize(
    "extra",
    [
        {"status": "failed"},
        {"status": None},
        {"status": "degraded"},
        {"fallback": True},
        {"pdf_no_text_layer": True},
        {"summary": None},
        {"summary": ""},
        {"best_guess_kind": None},
    ],
)
def test_latest_degraded_result_stays_visible(case, extra):
    conn, settings, event, result = case
    write_interpretation(
        conn, event=event, version=2, produced_by="extractor:new", extraction={**result, **extra}
    )
    assert index_repair.audit_indexes(conn, [event.id])["unsupported"] == 1
    assert index_repair.repair_indexes(conn, settings, [event.id])["skipped"] == 1
    assert conn.execute("SELECT COUNT(*) FROM pipeline_events").fetchone()[0] == 0


def test_other_hash_success_cannot_repair_original(case):
    conn, settings, _event, _result = case
    other = write_event(
        conn, origin="agent", kind="remember", payload={"content_type": "text", "text": "Other"}
    )
    # There is no extraction for other; a matching hash alone isn't authority.
    assert index_repair.repair_indexes(conn, settings, [other.id])["skipped"] == 1


@pytest.mark.parametrize(
    "error", [BackgroundBudgetExceeded("budget"), RuntimeError("sentinel secret")]
)
def test_failure_has_no_completion_and_retains_operational_visibility(case, monkeypatch, error):
    conn, settings, event, _ = case

    def fail(**kwargs):
        raise error

    monkeypatch.setattr(embedding, "embed_text", fail)
    stats = index_repair.repair_indexes(conn, settings, [event.id], embeddings=True)
    assert stats["deferred" if isinstance(error, BackgroundBudgetExceeded) else "failed"] == 1
    assert conn.execute("SELECT COUNT(*) FROM pipeline_events").fetchone()[0] == 0
    job = conn.execute("SELECT * FROM extraction_jobs WHERE event_id=?", (event.id,)).fetchone()
    assert job["last_error_type"] == type(error).__name__
    assert "sentinel" not in str(stats)


def test_fts_verification_failure_does_not_clear_gate(case, monkeypatch):
    conn, settings, event, _ = case
    monkeypatch.setattr(index_repair, "_enrich_fts_after_extraction", lambda *a, **kw: None)
    assert index_repair.repair_indexes(conn, settings, [event.id])["failed"] == 1
    assert conn.execute("SELECT COUNT(*) FROM pipeline_events").fetchone()[0] == 0


def test_rechecks_latest_success_under_lease(case, monkeypatch):
    conn, settings, event, result = case
    real = index_repair.extraction_lease

    @contextmanager
    def changed(*args, **kwargs):
        with real(*args, **kwargs):
            write_interpretation(
                conn,
                event=event,
                version=2,
                produced_by="extractor:new",
                extraction={**result, "status": "degraded"},
            )
            yield

    monkeypatch.setattr(index_repair, "extraction_lease", changed)
    assert index_repair.repair_indexes(conn, settings, [event.id])["deferred"] == 1
    assert conn.execute("SELECT COUNT(*) FROM pipeline_events").fetchone()[0] == 0
    job = conn.execute("SELECT * FROM extraction_jobs WHERE event_id=?", (event.id,)).fetchone()
    assert job is not None and job["last_error_type"] == "ExtractionDeferred"


@pytest.mark.parametrize("ids", [[], ["duplicate", "duplicate"], [str(i) for i in range(101)]])
def test_target_bound_fails_before_mutation(case, ids):
    conn, settings, _, _ = case
    before = conn.total_changes
    with pytest.raises(ValueError):
        index_repair.repair_indexes(conn, settings, ids)
    assert conn.total_changes == before


@pytest.mark.parametrize(
    "column,value",
    [
        ("terminal", 1),
        ("failures", 1),
        ("available_at", 9999999999),
        ("lease_until", 9999999999),
        ("last_error_type", "RuntimeError"),
    ],
)
def test_job_guards_do_not_reset_or_clear_visibility(case, column, value):
    conn, settings, event, _ = case
    with conn:
        conn.execute(
            f"INSERT INTO extraction_jobs(event_id,{column}) VALUES (?,?)", (event.id, value)
        )
    before = tuple(conn.execute("SELECT * FROM extraction_jobs").fetchone())
    assert index_repair.audit_indexes(conn, [event.id])["blocked"] == 1
    assert index_repair.repair_indexes(conn, settings, [event.id])["skipped"] == 1
    assert tuple(conn.execute("SELECT * FROM extraction_jobs").fetchone()) == before


def test_existing_vector_is_preserved_even_when_embedding_requested(case, monkeypatch):
    conn, settings, event, _ = case
    extractor._store_embedding(conn, event.content_hash, [0.25] * settings.embedding_dim)
    before = conn.execute(
        "SELECT embedding FROM events_vec WHERE content_hash=?", (event.content_hash,)
    ).fetchone()[0]
    monkeypatch.setattr(
        embedding, "embed_text", lambda **kw: pytest.fail("existing vector provider")
    )
    assert index_repair.repair_indexes(conn, settings, [event.id], embeddings=True)["verified"] == 1
    assert (
        conn.execute(
            "SELECT embedding FROM events_vec WHERE content_hash=?", (event.content_hash,)
        ).fetchone()[0]
        == before
    )


def test_bad_dimension_keeps_gap_visible(case, monkeypatch):
    conn, settings, event, _ = case
    monkeypatch.setattr(embedding, "embed_text", lambda **kw: [0.1])
    assert index_repair.repair_indexes(conn, settings, [event.id], embeddings=True)["failed"] == 1
    assert conn.execute("SELECT COUNT(*) FROM events_vec").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM pipeline_events").fetchone()[0] == 0


def test_lost_completion_write_is_not_reported_verified(case, monkeypatch):
    conn, settings, event, _ = case
    monkeypatch.setattr(pe, "record", lambda *a, **kw: None)
    assert index_repair.repair_indexes(conn, settings, [event.id])["failed"] == 1
    assert conn.execute("SELECT COUNT(*) FROM extraction_jobs WHERE failures=1").fetchone()[0] == 1


def test_observe_event_supported_and_binary_explicitly_unsupported(case):
    conn, settings, _, result = case
    observe = write_event(
        conn,
        origin="agent",
        kind="observe",
        payload={"content_type": "event", "action": "synthetic", "subject": "test"},
    )
    binary = write_event(
        conn,
        origin="agent",
        kind="remember",
        payload={"content_type": "binary", "filename_hint": "synthetic.pdf"},
    )
    for event in (observe, binary):
        write_interpretation(
            conn, event=event, version=1, produced_by="extractor:synthetic", extraction=result
        )
    stats = index_repair.repair_indexes(conn, settings, [observe.id, binary.id])
    assert stats["verified"] == 1 and stats["skipped"] == 1


def test_hash_mismatch_is_never_used(case):
    conn, settings, other, result = case
    event = write_event(
        conn,
        origin="agent",
        kind="remember",
        payload={"content_type": "text", "text": "Uninterpreted original"},
    )
    write_interpretation(
        conn,
        event=event.model_copy(update={"content_hash": other.content_hash}),
        version=1,
        produced_by="extractor:synthetic",
        extraction=result,
    )
    assert index_repair.audit_indexes(conn, [event.id])["unsupported"] == 1
    assert index_repair.repair_indexes(conn, settings, [event.id])["skipped"] == 1


def test_cli_default_opens_only_readonly_connection(case, monkeypatch, capsys):
    import runpy
    from pathlib import Path

    conn, settings, event, _ = case
    namespace = runpy.run_path(
        str(Path(__file__).parents[1] / "scripts/repair_extraction_indexes.py")
    )
    main = namespace["main"]
    monkeypatch.setitem(main.__globals__, "load_settings", lambda: settings)
    monkeypatch.setitem(main.__globals__, "_install_vault_key", lambda value: None)
    monkeypatch.setitem(
        main.__globals__, "open_db", lambda *a, **kw: pytest.fail("writable CLI open")
    )
    before = conn.total_changes
    assert main(["--event-id", event.id]) == 0
    assert '"fts_missing_or_different": 1' in capsys.readouterr().out
    assert conn.total_changes == before
    with pytest.raises(SystemExit):
        main(["--event-id", event.id, "--embeddings"])


def test_normal_embedding_path_records_embedding_operation_without_generation(case, monkeypatch):
    from types import SimpleNamespace

    import litellm

    conn, settings, event, _ = case
    calls = []

    def fake_embedding(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            data=[{"embedding": [0.1] * settings.embedding_dim}],
            usage={"prompt_tokens": 20, "total_tokens": 20},
        )

    monkeypatch.setattr(litellm, "embedding", fake_embedding)
    monkeypatch.setattr(litellm, "completion", lambda **kw: pytest.fail("generation model"))
    assert index_repair.repair_indexes(conn, settings, [event.id], embeddings=True)["verified"] == 1
    assert len(calls) == 1
    with sqlite3.connect(settings.vault_dir / "usage.db") as ledger:
        rows = ledger.execute("SELECT worker,operation,status FROM provider_usage").fetchall()
    assert len(rows) == 1
    assert rows[0][0:2] == ("index_repair", "embedding")


def test_budget_refusal_can_retry_fts_after_repair_backoff(case, monkeypatch):
    conn, settings, event, _ = case

    def refuse(**kwargs):
        raise BackgroundBudgetExceeded("budget")

    monkeypatch.setattr(embedding, "embed_text", refuse)
    assert index_repair.repair_indexes(conn, settings, [event.id], embeddings=True)["deferred"] == 1
    assert index_repair.repair_indexes(conn, settings, [event.id])["skipped"] == 1
    with conn:
        conn.execute("UPDATE extraction_jobs SET available_at=0 WHERE event_id=?", (event.id,))
    assert index_repair.repair_indexes(conn, settings, [event.id])["verified"] == 1
    assert conn.execute("SELECT COUNT(*) FROM pipeline_events").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM extraction_jobs").fetchone()[0] == 0


def test_three_failed_repairs_become_terminal(case, monkeypatch):
    conn, settings, event, _ = case
    monkeypatch.setattr(index_repair, "_enrich_fts_after_extraction", lambda *a, **kw: None)
    for _ in range(3):
        assert index_repair.repair_indexes(conn, settings, [event.id])["failed"] == 1
        with conn:
            conn.execute("UPDATE extraction_jobs SET available_at=0 WHERE event_id=?", (event.id,))
    assert conn.execute("SELECT terminal FROM extraction_jobs").fetchone()[0] == 1
    assert index_repair.repair_indexes(conn, settings, [event.id])["skipped"] == 1
    assert conn.execute("SELECT COUNT(*) FROM pipeline_events").fetchone()[0] == 0


def test_model_supplied_extracted_text_does_not_enter_inline_fts(case):
    conn, settings, event, result = case
    write_interpretation(
        conn,
        event=event,
        version=2,
        produced_by="extractor:new",
        extraction={**result, "extracted_text": "Injected model body sentinel"},
    )
    assert index_repair.repair_indexes(conn, settings, [event.id])["verified"] == 1
    assert (
        "Injected model body sentinel"
        not in conn.execute(
            "SELECT searchable_text FROM events_fts WHERE content_hash=?", (event.content_hash,)
        ).fetchone()[0]
    )


@pytest.mark.parametrize("malformed", ["[]", "null", '"a string"'])
def test_nonobject_interpretation_is_skipped_by_audit_and_apply(case, malformed):
    import json

    conn, settings, event, _ = case
    # Writer commits valid JSON before its reader model rejects a nonobject.
    with pytest.raises(ValidationError):
        write_interpretation(
            conn,
            event=event,
            version=2,
            produced_by="extractor:new",
            extraction=json.loads(malformed),
        )
    assert index_repair.audit_indexes(conn, [event.id])["unsupported"] == 1
    assert index_repair.repair_indexes(conn, settings, [event.id])["skipped"] == 1


def test_general_vector_repair_does_not_steal_index_repair_jobs(case, monkeypatch):
    from afair.agents.extraction_recovery import repair_missing_vectors

    conn, settings, event, _ = case
    with conn:
        conn.execute(
            "INSERT INTO extraction_jobs(event_id,purpose,failures) VALUES (?,'index_repair',1)",
            (event.id,),
        )
    monkeypatch.setattr(embedding, "embed_text", lambda **kw: pytest.fail("stolen repair"))
    assert repair_missing_vectors(conn, settings)["candidates"] == 0
    row = conn.execute("SELECT purpose,failures FROM extraction_jobs").fetchone()
    assert tuple(row) == ("index_repair", 1)
