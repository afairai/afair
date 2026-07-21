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
    is_structural_name,
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


# ── structural-junk detector (C) ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "DEV-69",
        "ADR-0004",
        "/admin/signups",
        "docs/self-hosting.md",
        "operations.md",
        "fly.dev.toml",
        "scripts/smoke_mcp.py",
    ],
)
def test_is_structural_name_true_for_paths_tickets_files(name: str) -> None:
    assert is_structural_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "Gowry",
        "maxime.team",  # bare domain → keeps its person→product retype (carve-out)
        "Menon 2011",
        "Claude Code",
        "afair",
    ],
)
def test_is_structural_name_false_for_real_entities(name: str) -> None:
    assert not is_structural_name(name)


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


def _entity(db: sqlite3.Connection, name: str, kind: str, *, split_homonym: bool = False) -> str:
    ev = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": name}
    )
    return write_entity(
        db,
        canonical_name=name,
        kind=kind,
        created_by="t",
        source_event_id=ev.id,
        confidence=0.8,
        split_homonym=split_homonym,
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


def test_worker_suppresses_structural_merge_review(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """C: a cross-kind merge of a structural name (a route path) files NO
    merge_review proposal — the operator shouldn't be nagged to pick a kind for
    '/admin/signups'. Natural names still propose."""
    _auto_merge_across_kinds(
        db, "/admin/signups", "project", "product", by="entity_deduplicator:v0"
    )
    _auto_merge_across_kinds(db, "Clario", "project", "product", by="entity_deduplicator:v0")

    stats = EntityAuditWorker().run(db, settings)
    assert stats["merge_review_proposals"] == 1  # only the natural name
    assert stats["suppressed_structural"] >= 1
    rows = db.execute(
        "SELECT detail FROM proposed_corrections WHERE kind = 'merge_review'"
    ).fetchall()
    names = {json.loads(r["detail"])["into_name"] for r in rows}
    assert names == {"Clario"}  # /admin/signups was suppressed


def test_worker_reviews_real_entity_merged_into_structural_name(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """C nit: suppression requires BOTH sides structural. A REAL entity merged
    INTO a structural name ('afair' project -> 'operations.md' product) is the
    merge most worth reviewing — it must still file a proposal (OR would have
    wrongly silenced it)."""
    from afair.substrate import write_entity_merge

    from_id = _entity(db, "afair", "project")  # real name
    into_id = _entity(db, "operations.md", "product")  # structural name
    write_entity_merge(
        db,
        from_entity_id=from_id,
        into_entity_id=into_id,
        merged_by="entity_deduplicator:v0",
        reason="t",
        confidence=0.9,
    )
    stats = EntityAuditWorker().run(db, settings)
    # The merge_review is filed (not suppressed): only ONE side is structural.
    assert stats["merge_review_proposals"] == 1
    row = db.execute(
        "SELECT detail FROM proposed_corrections WHERE kind = 'merge_review'"
    ).fetchone()
    assert json.loads(row["detail"])["into_name"] == "operations.md"


def test_worker_still_retypes_domain_person(db: sqlite3.Connection, settings: Settings) -> None:
    """C carve-out regression: 'maxime.team' is a bare domain, NOT a structural
    filename, so its person→product retype is STILL proposed (the domain
    carve-out is load-bearing)."""
    _entity(db, "maxime.team", "person")
    stats = EntityAuditWorker().run(db, settings)
    assert stats["retype_proposals"] == 1
    row = db.execute("SELECT detail FROM proposed_corrections WHERE kind = 'retype'").fetchone()
    detail = json.loads(row["detail"])
    assert detail["name"] == "maxime.team"
    assert detail["to_kind"] == "product"


def test_worker_suppresses_structural_person_retype(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """A person whose name is a file path is structural — no retype proposal,
    counted as suppressed."""
    _entity(db, "docs/self-hosting.md", "person")
    stats = EntityAuditWorker().run(db, settings)
    assert stats["retype_proposals"] == 0
    assert stats["suppressed_structural"] == 1


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


# ── semantic anti-re-nag (the Fable-x6 loop) ──────────────────────────────────


def test_semantic_key_suppresses_refiled_merge_review_after_decide(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """Regression for the Fable-x6 loop. The canonicalizer re-mints a NEW same-name
    cross-kind entity (fresh ULID) on every kind flip; the deduplicator auto-merges
    each fresh one into the same product-Fable. The (kind, from_entity_id) guard is
    keyed on the re-minted id, so it never blocks — the IDENTICAL question is
    re-proposed forever. The semantic key (from_name, from_kind, resolved into)
    suppresses the re-file even though from_entity_id differs.

    Red against the parent commit: without the semantic guard the second
    merge_review IS filed, so this asserts == 0 and dedup stat == 1.
    """
    # Cycle 1: person Fable auto-merged into product Fable → a merge_review filed.
    from1, into = _auto_merge_across_kinds(
        db, "Fable", "person", "product", by="entity_deduplicator:v0"
    )
    stats1 = EntityAuditWorker().run(db, settings)
    assert stats1["merge_review_proposals"] == 1
    # Operator decides it (rejects the auto-picked kind — the honest resolution).
    db.execute(
        "UPDATE proposed_corrections SET status = 'rejected', "
        "decided_at = '2026-01-01T00:00:00+00:00' WHERE entity_id = ?",
        (from1,),
    )
    db.commit()

    # Cycle 2: a FRESH person-Fable (new v2 disambiguator, distinct id) is minted
    # and auto-merged into the SAME product Fable — the canonicalizer's kind-flip
    # re-mint reproduced (split_homonym mints the next ordinal for the same name).
    from2 = _entity(db, "Fable", "person", split_homonym=True)
    write_entity_merge(
        db,
        from_entity_id=from2,
        into_entity_id=into,
        merged_by="entity_deduplicator:v0",
        reason="t",
        confidence=0.9,
    )
    assert from2 != from1
    # The detector STILL reports BOTH merges (each has a distinct from_entity_id,
    # both still cross-kind) — the raw signal the old guard could not collapse.
    assert len(find_cross_kind_auto_merges(db)) == 2

    stats2 = EntityAuditWorker().run(db, settings)
    # No new review: both merges (the decided from1 and the fresh from2) map to the
    # one already-decided question, so the semantic key suppresses both re-files.
    assert stats2["merge_review_proposals"] == 0
    assert stats2["merge_review_deduped_semantic"] == 2
    assert (
        db.execute(
            "SELECT COUNT(*) FROM proposed_corrections WHERE kind = 'merge_review'"
        ).fetchone()[0]
        == 1
    )


def test_semantic_key_absorbs_pending_twin_before_decide(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """Two fresh same-name cross-kind merges into the same target in ONE cycle
    (both still 'proposed') collapse to a single review — the semantic key sees
    the first-inserted row of ANY status."""
    _from1, into = _auto_merge_across_kinds(
        db, "Fable", "person", "product", by="entity_deduplicator:v0"
    )
    from2 = _entity(db, "Fable", "person", split_homonym=True)
    write_entity_merge(
        db,
        from_entity_id=from2,
        into_entity_id=into,
        merged_by="entity_deduplicator:v0",
        reason="t",
        confidence=0.9,
    )
    stats = EntityAuditWorker().run(db, settings)
    assert stats["merge_review_proposals"] == 1
    assert stats["merge_review_deduped_semantic"] == 1
    assert (
        db.execute(
            "SELECT COUNT(*) FROM proposed_corrections WHERE kind = 'merge_review'"
        ).fetchone()[0]
        == 1
    )


def test_semantic_key_does_not_suppress_distinct_target(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """A same-name person merged into a DIFFERENTLY-NAMED product is a genuinely
    new question — the resolved into-target differs, so it is not suppressed.

    (Two same-name-same-kind products share one v2 name-first id and ARE the same
    entity, so 'distinct target' means a distinct NAME, not a distinct instance.)"""
    _auto_merge_across_kinds(db, "Fable", "person", "product", by="entity_deduplicator:v0")
    # A fresh person Fable merged into a DIFFERENTLY-named product ('FableAI').
    from2 = _entity(db, "Fable", "person", split_homonym=True)
    into2 = _entity(db, "FableAI", "product")  # distinct name → distinct v2 id
    write_entity_merge(
        db,
        from_entity_id=from2,
        into_entity_id=into2,
        merged_by="entity_deduplicator:v0",
        reason="t",
        confidence=0.9,
    )
    stats = EntityAuditWorker().run(db, settings)
    assert stats["merge_review_proposals"] == 2  # both surface
    assert stats["merge_review_deduped_semantic"] == 0


def test_semantic_key_does_not_suppress_distinct_from_kind(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """A same-name entity of a DIFFERENT from-kind merged into the same target is
    a distinct question — not suppressed."""
    _, into = _auto_merge_across_kinds(
        db, "Fable", "person", "product", by="entity_deduplicator:v0"
    )
    # A concept Fable (distinct from-kind) merged into the same product Fable.
    from2 = _entity(db, "Fable", "concept")
    write_entity_merge(
        db,
        from_entity_id=from2,
        into_entity_id=into,
        merged_by="entity_deduplicator:v0",
        reason="t",
        confidence=0.9,
    )
    stats = EntityAuditWorker().run(db, settings)
    assert stats["merge_review_proposals"] == 2  # person-Fable and concept-Fable
    assert stats["merge_review_deduped_semantic"] == 0


def test_semantic_key_leaves_retype_path_unchanged(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """The semantic guard is merge_review-only; retype proposals still file and
    still honor the (kind, entity_id) NOT EXISTS idempotency."""
    _entity(db, "maxime.team", "person")  # retype -> product
    first = EntityAuditWorker().run(db, settings)
    assert first["retype_proposals"] == 1
    assert first["merge_review_deduped_semantic"] == 0
    second = EntityAuditWorker().run(db, settings)
    assert second["retype_proposals"] == 0  # unchanged idempotency


def test_decided_row_not_refiled_under_partial_index(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """P1-1 regression: with the partial unique index covering only OPEN rows, a
    DECIDED (confirmed) merge_review no longer blocks via the constraint — the
    explicit NOT EXISTS in _insert_proposal is what keeps the audit from
    re-nagging. Pins that a closed question stays closed (still cross-kind, so
    the detector still finds the merge)."""
    from_id, _into = _auto_merge_across_kinds(
        db, "Clario", "project", "product", by="entity_deduplicator:v0"
    )
    EntityAuditWorker().run(db, settings)
    # Operator confirmed the auto-picked kind — a decided, still-cross-kind row.
    db.execute(
        "UPDATE proposed_corrections SET status = 'confirmed', "
        "decided_at = '2026-01-01T00:00:00+00:00' WHERE entity_id = ?",
        (from_id,),
    )
    db.commit()
    # The detector STILL reports the merge as cross-kind (confirm wrote no kind).
    assert len(find_cross_kind_auto_merges(db)) == 1
    # But the worker files nothing new for it — the NOT EXISTS anti-re-nag guard.
    stats = EntityAuditWorker().run(db, settings)
    assert stats["merge_review_proposals"] == 0
    assert (
        db.execute(
            "SELECT COUNT(*) FROM proposed_corrections WHERE entity_id = ?", (from_id,)
        ).fetchone()[0]
        == 1
    )
