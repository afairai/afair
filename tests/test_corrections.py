"""Operator-confirmation surface for entity-audit proposals (ADR-0002).

Two layers:
  * substrate (``afair.substrate.corrections``) — read the open queue and
    decide a proposal, applying confirms through the append-only primitives;
  * handler wiring — the additive ``decide`` arg + ``pending_corrections``
    field on the frozen ``recall`` verb (I1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from afair.agents.entity_audit import EntityAuditWorker
from afair.mcp.schemas import CorrectionDecision
from afair.settings import Settings
from afair.substrate import (
    decide_correction,
    entity_id,
    open_db,
    read_pending_corrections,
    resolve_canonical,
    write_entity,
    write_event,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


# ── fixtures ─────────────────────────────────────────────────────────────────


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


def _entity(conn: sqlite3.Connection, name: str, kind: str) -> str:
    ev = write_event(
        conn, origin="user", kind="remember", payload={"content_type": "text", "text": name}
    )
    return write_entity(
        conn, canonical_name=name, kind=kind, created_by="t", source_event_id=ev.id, confidence=0.8
    ).id


def _seed_proposals(conn: sqlite3.Connection, settings: Settings) -> None:
    """Realistic seed: the same three error shapes the audit catches."""
    _entity(conn, "maxime.team", "person")  # → propose retype to product
    _entity(conn, "Bräuer", "person")  # ⊂ Dr. Gregor Bräuer → propose merge
    _entity(conn, "Dr. Gregor Bräuer", "person")
    EntityAuditWorker().run(conn, settings)


# ── read the queue ───────────────────────────────────────────────────────────


def test_read_pending_surfaces_open_proposals(db: sqlite3.Connection, settings: Settings) -> None:
    _seed_proposals(db, settings)
    pending = read_pending_corrections(db)
    kinds = sorted(p.kind for p in pending)
    assert kinds == ["merge", "retype"]
    # Most-confident first (retype domain = 0.9 outranks merge = 0.85).
    assert pending[0].kind == "retype"
    assert pending[0].entity_name == "maxime.team"
    assert "re-type" in pending[0].prompt


def test_pending_prompt_for_merge_names_both_sides(
    db: sqlite3.Connection, settings: Settings
) -> None:
    _seed_proposals(db, settings)
    merge = next(p for p in read_pending_corrections(db) if p.kind == "merge")
    assert "Bräuer" in merge.prompt
    assert "Dr. Gregor Bräuer" in merge.prompt
    assert "merge" in merge.prompt


# ── decide: confirm applies through the append-only path ─────────────────────


def test_confirm_retype_retypes_the_entity(db: sqlite3.Connection, settings: Settings) -> None:
    _seed_proposals(db, settings)
    retype = next(p for p in read_pending_corrections(db) if p.kind == "retype")
    person_id = entity_id("maxime.team", "person")
    product_id = entity_id("maxime.team", "product")

    out = decide_correction(db, proposal_id=retype.id, verdict="confirm")
    assert out.status == "applied"
    # The old person entity now resolves to the product-typed entity (a merge).
    assert resolve_canonical(db, person_id) == product_id
    # An observe event anchors the change (I7 — recorded).
    n_observe = db.execute("SELECT COUNT(*) FROM events WHERE kind = 'observe'").fetchone()[0]
    assert n_observe == 1
    # The proposal is closed and no longer surfaces.
    assert all(p.id != retype.id for p in read_pending_corrections(db))


def test_confirm_merge_merges_the_entities(db: sqlite3.Connection, settings: Settings) -> None:
    _seed_proposals(db, settings)
    merge = next(p for p in read_pending_corrections(db) if p.kind == "merge")
    braeuer_id = entity_id("Bräuer", "person")
    gregor_id = entity_id("Dr. Gregor Bräuer", "person")

    out = decide_correction(db, proposal_id=merge.id, verdict="confirm")
    assert out.status == "applied"
    assert resolve_canonical(db, braeuer_id) == gregor_id


# ── decide: reject closes untouched ──────────────────────────────────────────


def test_reject_closes_without_applying(db: sqlite3.Connection, settings: Settings) -> None:
    _seed_proposals(db, settings)
    retype = next(p for p in read_pending_corrections(db) if p.kind == "retype")
    person_id = entity_id("maxime.team", "person")

    out = decide_correction(db, proposal_id=retype.id, verdict="reject")
    assert out.status == "rejected"
    # Not re-typed — still its own canonical.
    assert resolve_canonical(db, person_id) == person_id
    # No observe event written on a reject.
    assert db.execute("SELECT COUNT(*) FROM events WHERE kind = 'observe'").fetchone()[0] == 0
    assert all(p.id != retype.id for p in read_pending_corrections(db))


# ── decide: idempotency + guards ─────────────────────────────────────────────


def test_decide_twice_is_a_no_op(db: sqlite3.Connection, settings: Settings) -> None:
    _seed_proposals(db, settings)
    retype = next(p for p in read_pending_corrections(db) if p.kind == "retype")

    first = decide_correction(db, proposal_id=retype.id, verdict="confirm")
    assert first.status == "applied"
    second = decide_correction(db, proposal_id=retype.id, verdict="confirm")
    assert second.status == "already_decided"
    # Only one merge from the re-type — no double-apply.
    n_merges = db.execute("SELECT COUNT(*) FROM entity_merges").fetchone()[0]
    assert n_merges == 1


def test_decide_unknown_proposal_reports_not_found(db: sqlite3.Connection) -> None:
    out = decide_correction(db, proposal_id="does-not-exist", verdict="confirm")
    assert out.status == "not_found"


def test_decide_rejects_bad_verdict(db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="verdict must be"):
        decide_correction(db, proposal_id="whatever", verdict="maybe")


# ── handler wiring on the frozen recall verb ─────────────────────────────────


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    from afair.mcp import handlers
    from afair.mcp.context import ServerContext, set_context

    vault = tmp_path / "vault"
    vault.mkdir()
    conn = open_db(vault)
    server_ctx = ServerContext(
        db=conn,
        vault_dir=vault,
        inline_text_max_bytes=64 * 1024,
        embedding_dim=1024,
        embedding_model="stub",
        surprise_context_window=20,
        semantic_recall_enabled=False,
    )
    set_context(server_ctx)
    monkeypatch.setattr(handlers, "connect_for_thread", lambda: conn)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=vault,
        cold_path_enabled=False,
    )
    _seed_proposals(conn, settings)
    try:
        yield server_ctx
    finally:
        conn.close()


def test_recall_stats_surfaces_pending_corrections(ctx: object) -> None:
    from afair.mcp import handlers

    result = handlers.recall(stats=True)
    assert {p.kind for p in result.pending_corrections} == {"retype", "merge"}


def test_recall_without_stats_omits_pending(ctx: object) -> None:
    from afair.mcp import handlers

    result = handlers.recall(query="anything")
    assert result.pending_corrections == []


def test_recall_decide_applies_and_reports(ctx: object) -> None:
    from afair.mcp import handlers

    proposals = handlers.recall(stats=True).pending_corrections
    retype = next(p for p in proposals if p.kind == "retype")

    result = handlers.recall(decide=CorrectionDecision(proposal_id=retype.id, verdict="confirm"))
    assert result.note is not None
    assert "confirm" in result.note
    person_id = entity_id("maxime.team", "person")
    product_id = entity_id("maxime.team", "product")
    db = ctx.db  # type: ignore[attr-defined]
    assert resolve_canonical(db, person_id) == product_id
    # The decided proposal is gone from the remaining queue echoed back.
    assert all(p.id != retype.id for p in result.pending_corrections)


def test_recall_decide_note_survives_a_combined_query(ctx: object) -> None:
    """A recall that both decides AND queries keeps the decide outcome in
    `note` — the query path's note must not clobber it."""
    from afair.mcp import handlers

    retype = next(p for p in handlers.recall(stats=True).pending_corrections if p.kind == "retype")
    result = handlers.recall(
        query="anything",
        decide=CorrectionDecision(proposal_id=retype.id, verdict="reject"),
    )
    assert result.note is not None
    assert "correction reject" in result.note
