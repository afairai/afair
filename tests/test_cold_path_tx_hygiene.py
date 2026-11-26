"""Every production cold-path worker returns with no write transaction open.

The scheduler shares one connection across all workers. A worker that returns
with an uncommitted write keeps SQLite's writer lock held into the next worker,
which made production ``remember`` calls fail with "database is locked". The
scheduler now commits such leftovers and logs them; this test keeps the roster
from relying on that safety net.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import litellm
import pytest

from afair.mcp.context import ServerContext, clear_context, set_context
from afair.mcp.server import build_cold_path_workers
from afair.settings import Settings
from afair.substrate import open_db, write_entity, write_entity_merge, write_event

if TYPE_CHECKING:
    from pathlib import Path


def _provider_down(*_args: Any, **_kwargs: Any) -> Any:
    msg = "LLM provider unavailable in tests"
    raise litellm.exceptions.APIConnectionError(message=msg, llm_provider="test", model="test")


def _seed(conn: Any) -> None:
    for text in ("Met Ana about the Clario launch", "Clario ships in March", "maxime.team"):
        event = write_event(
            conn, origin="user", kind="remember", payload={"content_type": "text", "text": text}
        )
        write_entity(
            conn,
            canonical_name=text.split()[0],
            kind="person",
            created_by="t",
            source_event_id=event.id,
            confidence=0.8,
        )
    source = write_event(
        conn, origin="user", kind="remember", payload={"content_type": "text", "text": "Clario"}
    )
    a = write_entity(
        conn,
        canonical_name="Clario",
        kind="project",
        created_by="t",
        source_event_id=source.id,
        confidence=0.8,
    )
    b = write_entity(
        conn,
        canonical_name="Clario",
        kind="product",
        created_by="t",
        source_event_id=source.id,
        confidence=0.8,
    )
    write_entity_merge(
        conn,
        from_entity_id=a.id,
        into_entity_id=b.id,
        merged_by="entity_deduplicator:v0",
        reason="t",
        confidence=0.9,
    )


@pytest.mark.parametrize("worker", build_cold_path_workers(), ids=lambda w: w.name)
def test_worker_returns_without_open_transaction(
    worker: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(litellm, "completion", _provider_down)
    monkeypatch.setattr(litellm, "embedding", _provider_down)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=tmp_path,
        cold_path_enabled=False,
    )
    conn = open_db(tmp_path)
    set_context(
        ServerContext(
            db=conn,
            vault_dir=tmp_path,
            inline_text_max_bytes=65536,
            semantic_recall_enabled=False,
            background_usage_settings=settings,
        )
    )
    monkeypatch.setattr("afair.substrate.watermarks._lag_threshold_id", lambda: "Z" * 26)
    monkeypatch.setattr("afair.agents.extraction_recovery.RECOVERY_GRACE_SECONDS", 0)
    try:
        _seed(conn)
        try:
            worker.run(conn, settings)
        except Exception:
            # Failed workers use the scheduler's rollback path; verify that
            # cleanup without skipping the production-roster conformance case.
            conn.rollback()
        assert not conn.in_transaction, f"{worker.name} left a write transaction open"
    finally:
        clear_context()
        conn.close()


@pytest.mark.parametrize("outcome", ["success", "budget", "invalid_format"])
def test_recovery_worker_transaction_hygiene(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    from afair.agents.extraction_recovery import ExtractionRecoveryWorker
    from afair.agents.llm import LLMResponseError, LLMResult
    from afair.agents.usage import BackgroundBudgetExceeded

    settings = Settings(_env_file=None, environment="local", vault_dir=tmp_path)
    conn = open_db(tmp_path)
    set_context(
        ServerContext(
            db=conn,
            vault_dir=tmp_path,
            inline_text_max_bytes=65536,
            semantic_recall_enabled=False,
            background_usage_settings=settings,
        )
    )
    monkeypatch.setattr("afair.substrate.watermarks._lag_threshold_id", lambda: "Z" * 26)
    monkeypatch.setattr("afair.agents.extraction_recovery.RECOVERY_GRACE_SECONDS", 0)

    def provider(**kwargs: Any) -> LLMResult:
        if outcome == "budget":
            raise BackgroundBudgetExceeded("deferred")
        if outcome == "invalid_format":
            raise LLMResponseError("invalid tool payload")
        return LLMResult(
            data={
                "summary": "A decision",
                "entities": [],
                "relations": [],
                "best_guess_kind": "note",
            },
            model="mock",
            raw="{}",
        )

    monkeypatch.setattr("afair.agents.extractor.call_tool", provider)
    try:
        _seed(conn)
        ExtractionRecoveryWorker().run(conn, settings)
        assert not conn.in_transaction
        other = open_db(tmp_path)
        try:
            other.execute("PRAGMA busy_timeout=0")
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
        finally:
            other.close()
    finally:
        clear_context()
        conn.close()
