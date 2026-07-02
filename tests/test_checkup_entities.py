"""ADR-0003 Phase 2 — read-only checkup script tests (Slice 1).

Seeds a small fixture vault (same pattern as test_backfill_entities.py),
then verifies the checkup report fields AND the hard read-only guarantee:
the connection the script opens must reject every write.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from afair.substrate import open_db, write_event
from afair.substrate import pipeline_events as pe
from afair.substrate.entities import (
    assign_entity_kind,
    write_entity,
    write_entity_mention,
)
from afair.substrate.kinds import write_kind_observation
from scripts.checkup_entities import _open_readonly, main, run_checkup

if TYPE_CHECKING:
    from pathlib import Path


def _entity_with_mention(conn: sqlite3.Connection, *, name: str, kind: str, split: bool = False):
    event = write_event(
        conn,
        origin="agent",
        kind="remember",
        payload={"content_type": "text", "text": f"{name} {kind} note"},
    )
    entity = write_entity(
        conn,
        canonical_name=name,
        kind=kind,
        created_by="test",
        source_event_id=event.id,
        confidence=0.9,
        split_homonym=split,
    )
    write_entity_mention(
        conn,
        entity_id=entity.id,
        event_id=event.id,
        event_hash=event.content_hash,
        surface_form=name,
        canonicalized_by="test",
        match_method="exact",
        confidence=1.0,
    )
    return entity, event


def _seed_vault(vault_dir: Path) -> None:
    conn = open_db(vault_dir)
    try:
        # Cross-kind cluster: two v2 entities, same name, different kinds.
        _entity_with_mention(conn, name="smoke.py", kind="product")
        _entity_with_mention(conn, name="smoke.py", kind="project")
        # Same-kind cluster: two v2 identities, same name+kind, via a split.
        _entity_with_mention(conn, name="alpha", kind="concept")
        _entity_with_mention(conn, name="alpha", kind="concept", split=True)
        # A standalone 'other'-kind entity with an exact mention (wildcard metric).
        widget, _ = _entity_with_mention(conn, name="widget", kind="other")
        # One kind assignment (retype overlay) + one kind observation.
        assign_entity_kind(
            conn,
            entity_id=widget.id,
            kind_slug="product",
            assigned_by="test",
            reason="checkup fixture retype",
            confidence=0.95,
        )
        write_kind_observation(
            conn,
            raw_kind="research_paper",
            normalized_slug="concept",
            entity_id=widget.id,
            event_id=widget.source_event_id,
            observed_by="test",
        )
        # A dedup-cycle pipeline marker so drain-rate has something to parse.
        pe.record(
            conn,
            event_id="-",
            stage="entity_dedup.cycle",
            producer="entity_deduplicator:v0",
            detail="examined=2 merged=1 entities_merged=1 kept_separate=1 "
            "skipped_operator_governed=0 skipped_recent=0 errors=0",
        )
    finally:
        conn.close()


def test_checkup_reports_all_fields(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    _seed_vault(vault)

    conn = _open_readonly(vault, None)
    try:
        report = run_checkup(conn)
    finally:
        conn.close()

    ic = report["identity_census"]
    # Every entity written here is v2 (write_entity mints v2 for new names).
    assert ic["total_entities"] == 5
    assert ic["v2_entities"] == 5
    assert ic["entity_identities"] == 5
    assert ic["entity_kind_assignments"] == 1
    assert ic["kind_observations"] == 1
    assert ic["kind_revisions"] == 0

    cc = report["cluster_census"]
    assert cc["total_clusters"] == 2
    assert cc["cross_kind_clusters"] == 1  # smoke.py (product vs project)
    assert cc["same_kind_clusters"] == 1  # alpha (concept vs concept)
    names = {c["name"] for c in cc["clusters"]}
    assert names == {"smoke.py", "alpha"}

    fr = report["formation_rate"]
    # smoke.py-project and the second alpha both have an older same-name sibling.
    assert fr["cross_kind_total"] >= 1
    assert sum(d["new_with_older_sibling"] for d in fr["per_day"]) >= 2

    dr = report["drain_rate"]
    assert dr["cycles"] == 1
    assert dr["per_day"][0]["examined"] == 2
    assert dr["per_day"][0]["merged"] == 1

    wm = report["wildcard_metric"]
    assert wm["exact_mentions"] == 5
    # widget was retyped away from 'other' → no exact mention resolves to 'other'.
    assert wm["other_kind_exact_mentions"] == 0


def test_checkup_connection_is_read_only(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    _seed_vault(vault)

    conn = _open_readonly(vault, None)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO entities (id, canonical_name, kind, created_at, "
                "created_by, confidence, source_event_id) "
                "VALUES ('x', 'x', 'other', 'now', 'test', 1.0, 'e')"
            )
    finally:
        conn.close()


def test_checkup_other_kind_exact_mention_counted(tmp_path: Path) -> None:
    """A live 'other'-kind entity with an exact mention shows in the metric."""
    vault = tmp_path / "vault"
    vault.mkdir()
    conn = open_db(vault)
    try:
        _entity_with_mention(conn, name="gizmo", kind="other")
    finally:
        conn.close()

    ro = _open_readonly(vault, None)
    try:
        report = run_checkup(ro)
    finally:
        ro.close()
    assert report["wildcard_metric"]["other_kind_exact_mentions"] == 1


def test_checkup_main_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    _seed_vault(vault)

    rc = main(["--vault-dir", str(vault)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "entity-graph checkup" in out
    assert "cluster census" in out


def test_checkup_main_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    _seed_vault(vault)

    rc = main(["--vault-dir", str(vault), "--json"])
    assert rc == 0
    import json

    report = json.loads(capsys.readouterr().out)
    assert report["identity_census"]["total_entities"] == 5


def test_checkup_missing_vault_returns_2(tmp_path: Path) -> None:
    rc = main(["--vault-dir", str(tmp_path / "nope")])
    assert rc == 2
