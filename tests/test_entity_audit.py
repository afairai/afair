"""Entity-audit worker — proposes corrections, never applies them (ADR-0002).

Covers the two detectors chosen after the live dry-run:
  * deterministic person type-mismatch (domain / citation-year), and
  * cross-kind auto-merge review (the deduplicator picked a kind across a
    boundary) — the main signal, surfaced for confirm/correct.

And the worker that queues them into proposed_corrections idempotently.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from afair.agents.entity_audit import (
    EntityAuditWorker,
    detect_type_mismatch,
    find_cross_kind_auto_merges,
)
from afair.settings import Settings
from afair.substrate import open_db, write_entity, write_entity_merge, write_event

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


# ── pure type detectors ──────────────────────────────────────────────────────


def test_detect_domain_person_as_product() -> None:
    out = detect_type_mismatch("maxime.team", "person")
    assert out is not None
    assert out[0] == "product"
    assert "domain" in out[1]


def test_detect_citation_person_as_concept() -> None:
    out = detect_type_mismatch("Menon 2011", "person")
    assert out is not None
    assert out[0] == "concept"


def test_detect_leaves_real_person_and_nonperson_alone() -> None:
    assert detect_type_mismatch("Sajinth", "person") is None
    assert detect_type_mismatch("Dr. Gregor Bräuer", "person") is None
    # Only audits person — a product named like a domain is fine as a product.
    assert detect_type_mismatch("maxime.team", "product") is None


# ── fixtures + helpers ───────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=tmp_path,
        cold_path_enabled=False,
    )


def _entity(db: sqlite3.Connection, name: str, kind: str) -> str:
    ev = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": name}
    )
    return write_entity(
        db, canonical_name=name, kind=kind, created_by="t", source_event_id=ev.id, confidence=0.8
    ).id


def _auto_merge_across_kinds(
    db: sqlite3.Connection, name: str, from_kind: str, to_kind: str, *, by: str
) -> tuple[str, str]:
    """Same name split across two kinds, merged by ``by`` — mirrors what the
    deduplicator does on the real vault (Clario project -> product)."""
    from_id = _entity(db, name, from_kind)
    into_id = _entity(db, name, to_kind)
    write_entity_merge(
        db, from_entity_id=from_id, into_entity_id=into_id, merged_by=by, reason="t", confidence=0.9
    )
    return from_id, into_id


# ── cross-kind auto-merge detector ───────────────────────────────────────────


def test_finds_automatic_cross_kind_merge(db: sqlite3.Connection) -> None:
    _auto_merge_across_kinds(db, "Clario", "project", "product", by="entity_deduplicator:v0")
    found = find_cross_kind_auto_merges(db)
    assert len(found) == 1
    _from_id, detail, _evidence, _conf = found[0]
    assert detail["from_kind"] == "project"
    assert detail["merged_kind"] == "product"
    assert detail["into_name"] == "Clario"


def test_ignores_operator_and_manual_merges(db: sqlite3.Connection) -> None:
    _auto_merge_across_kinds(db, "Maxime", "person", "product", by="operator")
    _auto_merge_across_kinds(db, "afair", "project", "product", by="manual:rename-x")
    assert find_cross_kind_auto_merges(db) == []


def test_ignores_same_kind_merge(db: sqlite3.Connection) -> None:
    # Two products merged into one product — no kind crossed, nothing to review.
    a = _entity(db, "bind agent", "product")
    b = _entity(db, "bind agent v0", "product")
    write_entity_merge(
        db,
        from_entity_id=a,
        into_entity_id=b,
        merged_by="entity_deduplicator:v0",
        reason="t",
        confidence=0.9,
    )
    assert find_cross_kind_auto_merges(db) == []


# ── the worker ───────────────────────────────────────────────────────────────


def test_worker_queues_retypes_and_merge_reviews(
    db: sqlite3.Connection, settings: Settings
) -> None:
    _entity(db, "maxime.team", "person")  # retype -> product
    _entity(db, "Menon 2011", "person")  # retype -> concept
    _entity(db, "Sajinth", "person")  # real — no proposal
    _auto_merge_across_kinds(db, "Clario", "project", "product", by="entity_deduplicator:v0")
    _auto_merge_across_kinds(db, "Maxime", "person", "product", by="operator")  # ignored

    stats = EntityAuditWorker().run(db, settings)
    assert stats["retype_proposals"] == 2
    assert stats["merge_review_proposals"] == 1

    rows = db.execute(
        "SELECT kind, detail, status FROM proposed_corrections ORDER BY kind"
    ).fetchall()
    kinds = sorted(r["kind"] for r in rows)
    assert kinds == ["merge_review", "retype", "retype"]
    mr = next(r for r in rows if r["kind"] == "merge_review")
    assert json.loads(mr["detail"])["into_name"] == "Clario"
    assert all(r["status"] == "proposed" for r in rows)


def test_worker_is_idempotent(db: sqlite3.Connection, settings: Settings) -> None:
    _auto_merge_across_kinds(db, "Clario", "project", "product", by="entity_deduplicator:v0")
    first = EntityAuditWorker().run(db, settings)
    assert first["merge_review_proposals"] == 1
    second = EntityAuditWorker().run(db, settings)
    assert second["merge_review_proposals"] == 0  # already proposed; not re-queued
    assert db.execute("SELECT COUNT(*) FROM proposed_corrections").fetchone()[0] == 1


def test_worker_skips_already_decided_proposal(db: sqlite3.Connection, settings: Settings) -> None:
    from_id, _into = _auto_merge_across_kinds(
        db, "Clario", "project", "product", by="entity_deduplicator:v0"
    )
    EntityAuditWorker().run(db, settings)
    db.execute(
        "UPDATE proposed_corrections SET status = 'confirmed' WHERE entity_id = ?", (from_id,)
    )
    db.commit()
    EntityAuditWorker().run(db, settings)
    rows = db.execute(
        "SELECT status FROM proposed_corrections WHERE entity_id = ?", (from_id,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "confirmed"  # untouched
