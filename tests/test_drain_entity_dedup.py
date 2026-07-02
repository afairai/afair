"""ADR-0003 Phase 2 completion — Slice 5: drain script smoke tests.

Fixture-vault only (never touches a real vault): seed same-name clusters,
mock the LLM judge, and verify the drain merges/keeps correctly, records
its observe audit anchor, and that --dry-run writes nothing.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from afair.agents import entity_dedup as ed
from afair.agents.llm import LLMResult
from afair.substrate import open_db, write_event
from afair.substrate.entities import entity_id, write_entity_mention
from scripts.drain_entity_dedup import main

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _seed_v1_cluster(db: Any, name: str) -> None:
    """A v1 same-name cross-kind cluster (product + project) with mentions —
    the realistic backlog shape the drain works down."""
    for kind in ("product", "project"):
        eid = entity_id(name, kind)
        event = write_event(
            db,
            origin="agent",
            kind="remember",
            payload={"content_type": "text", "text": f"{name} as a {kind}"},
        )
        with db:
            db.execute(
                """
                INSERT OR IGNORE INTO entities (
                    id, canonical_name, kind, created_at, created_by,
                    confidence, source_event_id
                ) VALUES (?, ?, ?, '2026-01-01T00:00:00+00:00', 'pre-phase2', 0.8, ?)
                """,
                (eid, name, kind, event.id),
            )
        write_entity_mention(
            db,
            entity_id=eid,
            event_id=event.id,
            event_hash=event.content_hash,
            surface_form=name,
            canonicalized_by="test",
            match_method="exact",
            confidence=0.9,
        )


def _stub_judge_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """same_entity=True unless the record names a 'keep' cluster."""

    def _call(**kwargs: Any) -> LLMResult:
        user = str(kwargs.get("user", ""))
        same = "keep" not in user
        return LLMResult(
            data={
                "same_entity": same,
                "reason": "stub",
                "confidence": 0.95,
                "unified_kind": "product" if same else None,
            },
            model="stub",
            raw="{}",
        )

    monkeypatch.setattr(ed, "call_tool", _call)


def _merge_count(db: Any) -> int:
    return int(db.execute("SELECT COUNT(*) AS n FROM entity_merges").fetchone()["n"])


def _drain_observe(db: Any) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT payload FROM events WHERE kind = 'observe' "
        "AND json_extract(payload, '$.action') = 'drain_entity_dedup'"
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def test_drain_merges_and_keeps_and_writes_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = open_db(tmp_path)
    for i in range(3):
        _seed_v1_cluster(db, f"merge{i}")
    for i in range(2):
        _seed_v1_cluster(db, f"keep{i}")
    db.close()

    _stub_judge_by_name(monkeypatch)
    rc = main(["--vault-dir", str(tmp_path), "--max-clusters", "25", "--sleep", "0", "--quiet"])
    assert rc == 0

    db2 = open_db(tmp_path)
    try:
        # 3 merge clusters → 3 merges; 2 keep clusters untouched.
        assert _merge_count(db2) == 3
        observes = _drain_observe(db2)
        assert len(observes) == 1
        payload = observes[0]
        assert payload["result"] == "completed"
        assert payload["clusters_merged"] == 3
        assert payload["entities_merged"] == 3
        assert payload["skipped_not_same"] == 2
        assert "cycles_run" in payload
        assert "duration_seconds" in payload
    finally:
        db2.close()


def test_drain_dry_run_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = open_db(tmp_path)
    for i in range(3):
        _seed_v1_cluster(db, f"merge{i}")
    db.close()

    # If the LLM is called during a dry run, that's a bug.
    def _boom(**_: Any) -> LLMResult:
        msg = "dry-run must not call the LLM"
        raise AssertionError(msg)

    monkeypatch.setattr(ed, "call_tool", _boom)

    rc = main(["--vault-dir", str(tmp_path), "--dry-run"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "dry-run: no LLM calls, no writes." in out
    assert "same-name cluster" in out

    db2 = open_db(tmp_path)
    try:
        assert _merge_count(db2) == 0  # nothing merged
        assert _drain_observe(db2) == []  # no audit anchor
    finally:
        db2.close()


def test_drain_missing_vault_returns_2(tmp_path: Path) -> None:
    rc = main(["--vault-dir", str(tmp_path / "nope"), "--quiet"])
    assert rc == 2


def test_drain_respects_max_clusters_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The total examined never exceeds --max-clusters in one invocation."""
    db = open_db(tmp_path)
    for i in range(5):
        _seed_v1_cluster(db, f"merge{i}")
    db.close()

    _stub_judge_by_name(monkeypatch)
    rc = main(["--vault-dir", str(tmp_path), "--max-clusters", "2", "--sleep", "0", "--quiet"])
    assert rc == 0

    db2 = open_db(tmp_path)
    try:
        # Only 2 clusters examined → at most 2 merges this batch.
        assert _merge_count(db2) == 2
        payload = _drain_observe(db2)[0]
        assert payload["clusters_examined"] == 2
    finally:
        db2.close()
