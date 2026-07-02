"""ADR-0003 Phase 2 completion — Slice 3: dedup kind unification.

A confidently same-entity cluster's kind disagreement becomes ONE
kind-assignment row per divergent member (fully attributed, one-row
reversible), gated at KIND_UNIFY_CONFIDENCE=0.9. Below the threshold, or
for an out-of-menu kind, today's behavior is preserved exactly: the merge
lands, the kind disagreement stands, and entity_audit files a merge_review.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from afair.agents import entity_dedup as ed
from afair.agents.entity_audit import EntityAuditWorker
from afair.agents.llm import LLMResult
from afair.settings import Settings
from afair.substrate import open_db, write_event
from afair.substrate.entities import (
    assign_entity_kind,
    entity_id,
    resolve_canonical,
    resolve_entity_kind,
    write_entity_mention,
)

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


@pytest.fixture
def conn(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    c = open_db(vault)
    yield c
    c.close()


def _seed_entity(conn: sqlite3.Connection, *, name: str, kind: str, n_mentions: int) -> str:
    """Seed a v1 (kind-in-hash) entity — the realistic dedup backlog, and
    clear of the Slice-4 deliberate-split guard (which only fires on v2
    split identities)."""
    eid = entity_id(name, kind)
    for i in range(n_mentions):
        event = write_event(
            conn,
            origin="agent",
            kind="remember",
            payload={"content_type": "text", "text": f"{name} {kind} note {i}"},
        )
        with conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO entities (
                    id, canonical_name, kind, created_at, created_by,
                    confidence, source_event_id
                ) VALUES (?, ?, ?, '2026-01-01T00:00:00+00:00', 'test', 0.9, ?)
                """,
                (eid, name, kind, event.id),
            )
        write_entity_mention(
            conn,
            entity_id=eid,
            event_id=event.id,
            event_hash=event.content_hash,
            surface_form=name,
            canonicalized_by="test",
            match_method="exact",
            confidence=0.9,
        )
    return eid


def _stub_judge(monkeypatch, *, same: bool, confidence: float, unified_kind: str | None) -> None:
    def _call(**_kwargs):
        return LLMResult(
            data={
                "same_entity": same,
                "reason": "stub",
                "confidence": confidence,
                "unified_kind": unified_kind,
            },
            model="stub",
            raw="{}",
        )

    monkeypatch.setattr(ed, "call_tool", _call)


def _merge_review_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM proposed_corrections WHERE kind = 'merge_review'"
        ).fetchone()["n"]
    )


def test_unified_kind_applied_at_high_confidence(conn, monkeypatch) -> None:
    product = _seed_entity(conn, name="smoke.py", kind="product", n_mentions=3)
    project = _seed_entity(conn, name="smoke.py", kind="project", n_mentions=1)
    _stub_judge(monkeypatch, same=True, confidence=0.95, unified_kind="product")

    stats = ed.EntityDeduplicator().run(conn, Settings())

    assert stats["clusters_merged"] == 1
    assert stats["kinds_unified"] == 1
    # The project member was reassigned to product; both now resolve to product.
    assert resolve_entity_kind(conn, project) == "product"
    assert resolve_entity_kind(conn, product) == "product"


def test_unified_kind_not_applied_below_threshold(conn, monkeypatch) -> None:
    _seed_entity(conn, name="smoke.py", kind="product", n_mentions=3)
    project = _seed_entity(conn, name="smoke.py", kind="project", n_mentions=1)
    # 0.85: above the merge floor (0.75), below KIND_UNIFY_CONFIDENCE (0.9).
    _stub_judge(monkeypatch, same=True, confidence=0.85, unified_kind="product")

    stats = ed.EntityDeduplicator().run(conn, Settings())

    assert stats["clusters_merged"] == 1  # merge still lands
    assert stats["kinds_unified"] == 0  # but no kind assignment
    assert resolve_entity_kind(conn, project) == "project"  # unchanged


def test_out_of_menu_unified_kind_discarded(conn, monkeypatch) -> None:
    """A unified_kind not shown in the records is discarded (I6, Security L1):
    the merge still happens, no kind is invented, and review still fires."""
    product = _seed_entity(conn, name="smoke.py", kind="product", n_mentions=3)
    project = _seed_entity(conn, name="smoke.py", kind="project", n_mentions=1)
    _stub_judge(monkeypatch, same=True, confidence=0.95, unified_kind="banana")

    stats = ed.EntityDeduplicator().run(conn, Settings())

    assert stats["clusters_merged"] == 1
    assert stats["kinds_unified"] == 0
    assert resolve_entity_kind(conn, project) == "project"  # not touched
    assert resolve_canonical(conn, project) == product  # merged though

    # A cross-kind merge with no unification → merge_review is filed.
    EntityAuditWorker().run(conn, Settings())
    assert _merge_review_count(conn) == 1


def test_merge_review_suppressed_for_unified_cluster(conn, monkeypatch) -> None:
    _seed_entity(conn, name="smoke.py", kind="product", n_mentions=3)
    _seed_entity(conn, name="smoke.py", kind="project", n_mentions=1)
    _stub_judge(monkeypatch, same=True, confidence=0.95, unified_kind="product")

    ed.EntityDeduplicator().run(conn, Settings())
    EntityAuditWorker().run(conn, Settings())

    # Unified cluster shows equal current kinds on both sides → no review.
    assert _merge_review_count(conn) == 0


def test_merge_review_filed_for_non_unified_cluster(conn, monkeypatch) -> None:
    _seed_entity(conn, name="foo", kind="product", n_mentions=3)
    _seed_entity(conn, name="foo", kind="project", n_mentions=1)
    # Merges (>=0.75) but no unified_kind → kind disagreement stands.
    _stub_judge(monkeypatch, same=True, confidence=0.8, unified_kind=None)

    ed.EntityDeduplicator().run(conn, Settings())
    EntityAuditWorker().run(conn, Settings())

    assert _merge_review_count(conn) == 1


def test_operator_kind_revert_not_overridden_next_cycle(conn, monkeypatch) -> None:
    """An operator retype after a dedup unification stands: the merged cluster
    is skipped as already-collapsed next cycle, and latest-row-wins keeps the
    operator's assignment on top regardless (ADR-0002 entrenchment, I7)."""
    _seed_entity(conn, name="smoke.py", kind="product", n_mentions=3)
    project = _seed_entity(conn, name="smoke.py", kind="project", n_mentions=1)
    _stub_judge(monkeypatch, same=True, confidence=0.95, unified_kind="product")

    ed.EntityDeduplicator().run(conn, Settings())
    assert resolve_entity_kind(conn, project) == "product"

    # Operator reverts the kind of the merged-away member back to project.
    assign_entity_kind(
        conn,
        entity_id=project,
        kind_slug="project",
        assigned_by="operator",
        reason="operator retype: it really is a project",
        confidence=1.0,
    )
    assert resolve_entity_kind(conn, project) == "project"

    stats2 = ed.EntityDeduplicator().run(conn, Settings())
    assert stats2["skipped_already_merged"] >= 1
    assert stats2["kinds_unified"] == 0
    # Operator's revert stands.
    assert resolve_entity_kind(conn, project) == "project"
