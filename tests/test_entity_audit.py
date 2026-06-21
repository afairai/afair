"""Entity-audit worker — proposes corrections, never applies them (ADR-0002).

Covers the deterministic detectors (the same patterns behind the real errors:
a domain filed as a person, a citation filed as a person, a surface-form
duplicate) and the worker that queues them into proposed_corrections
idempotently.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from afair.agents.entity_audit import (
    EntityAuditWorker,
    detect_type_mismatch,
    find_merge_candidates,
)
from afair.settings import Settings
from afair.substrate import open_db, write_entity, write_event

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


# ── pure detectors ───────────────────────────────────────────────────────────


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


def test_find_merge_candidates_shorter_into_fuller() -> None:
    ents = [
        ("e:braeuer", "Bräuer", "person"),
        ("e:gregor", "Dr. Gregor Bräuer", "person"),
        ("e:sajinth", "Sajinth", "person"),
    ]
    cands = find_merge_candidates(ents)
    assert len(cands) == 1
    from_id, into_id, _evidence, _conf = cands[0]
    assert from_id == "e:braeuer"  # the shorter form merges INTO the fuller name
    assert into_id == "e:gregor"


def test_merge_candidates_ignore_different_kinds() -> None:
    ents = [("e:a", "Apple", "organization"), ("e:b", "Apple Pie", "product")]
    assert find_merge_candidates(ents) == []


# ── the worker ───────────────────────────────────────────────────────────────


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


def test_worker_queues_the_three_error_shapes(db: sqlite3.Connection, settings: Settings) -> None:
    maxime = _entity(db, "maxime.team", "person")
    _entity(db, "Menon 2011", "person")
    braeuer = _entity(db, "Bräuer", "person")
    _entity(db, "Dr. Gregor Bräuer", "person")
    _entity(db, "Sajinth", "person")  # real — no proposal

    stats = EntityAuditWorker().run(db, settings)
    assert stats["retype_proposals"] == 2  # maxime.team, Menon 2011
    assert stats["merge_proposals"] == 1  # Bräuer → Dr. Gregor Bräuer

    rows = db.execute(
        "SELECT kind, entity_id, detail, status FROM proposed_corrections ORDER BY kind"
    ).fetchall()
    assert len(rows) == 3
    # The Maxime retype proposes product.
    retypes = {r["entity_id"]: json.loads(r["detail"]) for r in rows if r["kind"] == "retype"}
    assert retypes[maxime]["to_kind"] == "product"
    # The merge points the shorter form at the fuller name.
    merge = next(r for r in rows if r["kind"] == "merge")
    assert merge["entity_id"] == braeuer
    assert all(r["status"] == "proposed" for r in rows)


def test_worker_is_idempotent(db: sqlite3.Connection, settings: Settings) -> None:
    _entity(db, "maxime.team", "person")
    first = EntityAuditWorker().run(db, settings)
    assert first["retype_proposals"] == 1
    second = EntityAuditWorker().run(db, settings)
    assert second["retype_proposals"] == 0  # already proposed; not re-queued
    assert db.execute("SELECT COUNT(*) FROM proposed_corrections").fetchone()[0] == 1


def test_worker_skips_already_decided_proposal(db: sqlite3.Connection, settings: Settings) -> None:
    eid = _entity(db, "maxime.team", "person")
    EntityAuditWorker().run(db, settings)
    # Operator rejected it — the audit must not re-open it.
    db.execute("UPDATE proposed_corrections SET status = 'rejected' WHERE entity_id = ?", (eid,))
    db.commit()
    EntityAuditWorker().run(db, settings)
    rows = db.execute(
        "SELECT status FROM proposed_corrections WHERE entity_id = ?", (eid,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "rejected"  # untouched
