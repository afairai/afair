"""Operator-confirmed ontology revisions (ADR-0003 Phase 5).

Three layers:
  * substrate (``afair.substrate.ontology``) — read the open queue, decide a
    proposal, apply confirms through the append-only kind primitives
    (``kind_registry`` / ``kind_revisions`` / ``entity_kind_assignments``),
    and revert applied revisions with compensating rows (I7);
  * dispatch — ``decide_correction`` routes ``ont_``-prefixed ids to the
    ontology path without breaking entity-correction decisions (I1: one
    verb, one argument, two queues);
  * surfacing — ontology proposals join ``pending_corrections`` on
    ``recall(stats=True)`` and the ``afair://session-start`` resource.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from ulid import ULID

from afair.mcp.schemas import CorrectionDecision
from afair.substrate import (
    decide_correction,
    live_kind_slugs,
    open_db,
    read_pending_ontology_proposals,
    resolve_entity_kind,
    resolve_kind_slug,
    write_entity,
    write_event,
)
from afair.substrate.kinds import ONTOLOGY_PROPOSAL_ID_PREFIX

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


# ── fixtures + helpers ───────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path)
    try:
        yield conn
    finally:
        conn.close()


def _entity(conn: sqlite3.Connection, name: str, kind: str) -> str:
    ev = write_event(
        conn, origin="user", kind="remember", payload={"content_type": "text", "text": name}
    )
    return write_entity(
        conn, canonical_name=name, kind=kind, created_by="t", source_event_id=ev.id, confidence=0.8
    ).id


def _proposal(
    conn: sqlite3.Connection,
    *,
    action: str,
    subject_slug: str,
    detail: dict[str, Any],
    confidence: float = 0.8,
    evidence: str = "test signal",
) -> str:
    """Insert one queue row the way the Schema-Evolver does (Phase 4 shape:
    for 'add' the subject IS the proposed new slug, source in
    detail.source_slug)."""
    proposal_id = f"{ONTOLOGY_PROPOSAL_ID_PREFIX}{ULID()!s}"
    with conn:
        conn.execute(
            """
            INSERT INTO proposed_ontology_revisions (
                id, action, subject_slug, detail, evidence, confidence,
                detected_by, detected_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, 'schema_evolver:v0',
                      '2026-07-01T00:00:00+00:00', 'proposed')
            """,
            (proposal_id, action, subject_slug, json.dumps(detail), evidence, confidence),
        )
    return proposal_id


def _queue_row(conn: sqlite3.Connection, proposal_id: str) -> Any:
    return conn.execute(
        "SELECT status, decided_at, decided_by FROM proposed_ontology_revisions WHERE id = ?",
        (proposal_id,),
    ).fetchone()


def _observe_events(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT payload FROM events WHERE kind = 'observe'").fetchall()
    return [json.loads(r["payload"]) for r in rows]


def _revision_rows(conn: sqlite3.Connection) -> list[Any]:
    return conn.execute(
        "SELECT action, from_slug, to_slug, source_event_id FROM kind_revisions "
        "ORDER BY revised_at, id"
    ).fetchall()


# ── the pending reader ───────────────────────────────────────────────────────


def test_read_pending_surfaces_open_proposals_most_confident_first(
    db: sqlite3.Connection,
) -> None:
    _proposal(
        db,
        action="add",
        subject_slug="research_paper",
        detail={"source_slug": "other", "label": "Research paper"},
        confidence=0.9,
    )
    _proposal(
        db, action="deprecate", subject_slug="place", detail={"slug": "place"}, confidence=0.5
    )
    pending = read_pending_ontology_proposals(db)
    assert [p.action for p in pending] == ["add", "deprecate"]
    assert pending[0].subject_slug == "research_paper"
    assert "research_paper" in pending[0].prompt
    assert "place" in pending[1].prompt


# ── confirm: each action flips the resolved view ─────────────────────────────


def test_confirm_add_registers_kind_and_reassigns_entities(db: sqlite3.Connection) -> None:
    """Phase-4 'add' shape: subject_slug is the PROPOSED slug; the entities
    that move ride detail.reassign_entity_ids."""
    moved = _entity(db, "Attention Is All You Need", "other")
    stays = _entity(db, "misc thing", "other")
    pid = _proposal(
        db,
        action="add",
        subject_slug="research_paper",
        detail={
            "source_slug": "other",
            "label": "Research paper",
            "description": "Published academic papers.",
            "reassign_entity_ids": [moved],
        },
    )

    out = decide_correction(db, proposal_id=pid, verdict="confirm")
    assert out.status == "applied"
    assert "research_paper" in out.note

    # The kind is live; the registry row + 'add' revision landed.
    assert "research_paper" in live_kind_slugs(db)
    reg = db.execute(
        "SELECT label, created_by FROM kind_registry WHERE slug = 'research_paper'"
    ).fetchone()
    assert reg["label"] == "Research paper"
    assert reg["created_by"] == "operator"
    revs = _revision_rows(db)
    assert [(r["action"], r["to_slug"]) for r in revs] == [("add", "research_paper")]
    # Anchored to an observe event (I7 — recorded), referenced downstream.
    assert revs[0]["source_event_id"] is not None
    payloads = _observe_events(db)
    assert len(payloads) == 1
    assert payloads[0]["action"] == "apply_ontology_revision"
    assert payloads[0]["subject"] == pid
    # The reassigned entity resolves to the new kind; the other stays put.
    assert resolve_entity_kind(db, moved) == "research_paper"
    assert resolve_entity_kind(db, stays) == "other"
    # Queue row flipped; the proposal no longer surfaces.
    row = _queue_row(db, pid)
    assert row["status"] == "applied"
    assert row["decided_at"] is not None
    assert row["decided_by"] == "operator"
    assert all(p.id != pid for p in read_pending_ontology_proposals(db))
    # The ADR's pipeline stamp.
    stage = db.execute(
        "SELECT COUNT(*) FROM pipeline_events WHERE stage = 'schema_evolver.applied'"
    ).fetchone()[0]
    assert stage == 1


def test_confirm_rename_retypes_population_at_read_time(db: sqlite3.Connection) -> None:
    """A registry-level rename is ONE revision row; every entity of the old
    slug resolves to the new one with zero per-entity writes."""
    org = _entity(db, "Anthropic", "organization")
    pid = _proposal(
        db,
        action="rename",
        subject_slug="organization",
        detail={"from_slug": "organization", "to_slug": "company", "to_label": "Company"},
    )

    out = decide_correction(db, proposal_id=pid, verdict="confirm")
    assert out.status == "applied"
    assert resolve_kind_slug(db, "organization") == "company"
    live = live_kind_slugs(db)
    assert "company" in live
    assert "organization" not in live
    # The population retypes at read time — no assignment rows were written.
    assert resolve_entity_kind(db, org) == "company"
    assert db.execute("SELECT COUNT(*) FROM entity_kind_assignments").fetchone()[0] == 0


def test_confirm_merge_retypes_population_at_read_time(db: sqlite3.Connection) -> None:
    prod = _entity(db, "Clario", "product")
    pid = _proposal(
        db,
        action="merge",
        subject_slug="product",
        detail={"from_slug": "product", "to_slug": "project", "signal": "co_occurrence"},
    )

    out = decide_correction(db, proposal_id=pid, verdict="confirm")
    assert out.status == "applied"
    assert resolve_kind_slug(db, "product") == "project"
    assert "product" not in live_kind_slugs(db)
    assert resolve_entity_kind(db, prod) == "project"
    assert db.execute("SELECT COUNT(*) FROM entity_kind_assignments").fetchone()[0] == 0
    revs = _revision_rows(db)
    assert [(r["action"], r["from_slug"], r["to_slug"]) for r in revs] == [
        ("merge", "product", "project")
    ]


def test_confirm_deprecate_retires_the_kind(db: sqlite3.Connection) -> None:
    pid = _proposal(db, action="deprecate", subject_slug="place", detail={"slug": "place"})
    out = decide_correction(db, proposal_id=pid, verdict="confirm")
    assert out.status == "applied"
    assert "place" not in live_kind_slugs(db)
    # The registry row stays forever (history); liveness is revision state.
    assert db.execute("SELECT 1 FROM kind_registry WHERE slug = 'place'").fetchone() is not None


def test_confirm_split_covers_stragglers_with_the_default_successor(
    db: sqlite3.Connection,
) -> None:
    classified = _entity(db, "Attention Is All You Need", "concept")
    straggler = _entity(db, "stoicism", "concept")
    pid = _proposal(
        db,
        action="split",
        subject_slug="concept",
        detail={
            "from_slug": "concept",
            "successors": [
                {"slug": "research_paper", "label": "Research paper"},
                {"slug": "idea", "label": "Idea"},
            ],
            "default_slug": "idea",
            "assignments": {classified: "research_paper"},
        },
    )

    out = decide_correction(db, proposal_id=pid, verdict="confirm")
    assert out.status == "applied"
    live = live_kind_slugs(db)
    assert "research_paper" in live
    assert "idea" in live
    assert "concept" not in live
    # The classified entity moved explicitly; the straggler resolves to the
    # default successor at read time (no assignment row of its own).
    assert resolve_entity_kind(db, classified) == "research_paper"
    assert resolve_entity_kind(db, straggler) == "idea"
    n_assignments = db.execute("SELECT COUNT(*) FROM entity_kind_assignments").fetchone()[0]
    assert n_assignments == 1
    # History: one 'split' row per successor; the default marked in detail.
    split_rows = db.execute(
        "SELECT to_slug, detail FROM kind_revisions WHERE action = 'split' ORDER BY to_slug"
    ).fetchall()
    assert [(r["to_slug"], json.loads(r["detail"])["default"]) for r in split_rows] == [
        ("idea", True),
        ("research_paper", False),
    ]


# ── reject / idempotency ─────────────────────────────────────────────────────


def test_reject_leaves_the_live_ontology_unchanged(db: sqlite3.Connection) -> None:
    before = live_kind_slugs(db)
    pid = _proposal(
        db,
        action="add",
        subject_slug="research_paper",
        detail={"source_slug": "other", "label": "Research paper"},
    )
    out = decide_correction(db, proposal_id=pid, verdict="reject")
    assert out.status == "rejected"
    assert live_kind_slugs(db) == before
    assert db.execute("SELECT COUNT(*) FROM kind_revisions").fetchone()[0] == 0
    assert (
        db.execute("SELECT 1 FROM kind_registry WHERE slug = 'research_paper'").fetchone() is None
    )
    assert _observe_events(db) == []
    assert _queue_row(db, pid)["status"] == "rejected"


def test_double_decide_reports_already_decided(db: sqlite3.Connection) -> None:
    pid = _proposal(db, action="deprecate", subject_slug="place", detail={"slug": "place"})
    assert decide_correction(db, proposal_id=pid, verdict="confirm").status == "applied"
    again = decide_correction(db, proposal_id=pid, verdict="confirm")
    assert again.status == "already_decided"
    # Exactly one deprecate revision — the retry never double-applied.
    assert db.execute("SELECT COUNT(*) FROM kind_revisions").fetchone()[0] == 1


def test_unknown_ontology_id_reports_not_found(db: sqlite3.Connection) -> None:
    out = decide_correction(db, proposal_id="ont_01UNKNOWN", verdict="confirm")
    assert out.status == "not_found"


def test_noop_confirm_reports_confirmed_not_applied(db: sqlite3.Connection) -> None:
    """Confirming an add whose slug is already live writes nothing — status
    'confirmed' (so a later revert has nothing to compensate)."""
    pid = _proposal(
        db, action="add", subject_slug="person", detail={"source_slug": "other", "label": "Person"}
    )
    out = decide_correction(db, proposal_id=pid, verdict="confirm")
    assert out.status == "confirmed"
    assert "no-op" in out.note
    assert db.execute("SELECT COUNT(*) FROM kind_revisions").fetchone()[0] == 0
    assert _observe_events(db) == []
    # Revert refuses: nothing was applied.
    rev = decide_correction(db, proposal_id=pid, verdict="revert")
    assert rev.status == "not_applied"


# ── revert (I7 — compensating rows through the same loop) ────────────────────


def test_revert_restores_a_confirmed_deprecate(db: sqlite3.Connection) -> None:
    pid = _proposal(db, action="deprecate", subject_slug="place", detail={"slug": "place"})
    decide_correction(db, proposal_id=pid, verdict="confirm")
    assert "place" not in live_kind_slugs(db)

    out = decide_correction(db, proposal_id=pid, verdict="revert")
    assert out.status == "reverted"
    assert "place" in live_kind_slugs(db)
    # Compensating row, never a mutation: deprecate AND restore both stand.
    actions = [r["action"] for r in _revision_rows(db)]
    assert actions == ["deprecate", "restore"]
    # Both the apply and the revert are anchored to observe events.
    assert [p["action"] for p in _observe_events(db)] == [
        "apply_ontology_revision",
        "revert_ontology_revision",
    ]
    # A second revert is a semantic no-op (latest-row-wins already holds).
    again = decide_correction(db, proposal_id=pid, verdict="revert")
    assert again.status == "reverted"
    assert "no-op" in again.note
    assert [r["action"] for r in _revision_rows(db)] == ["deprecate", "restore"]


def test_revert_rename_ends_the_redirect_chain(db: sqlite3.Connection) -> None:
    org = _entity(db, "Anthropic", "organization")
    pid = _proposal(
        db,
        action="rename",
        subject_slug="organization",
        detail={"from_slug": "organization", "to_slug": "company"},
    )
    decide_correction(db, proposal_id=pid, verdict="confirm")
    assert resolve_entity_kind(db, org) == "company"

    out = decide_correction(db, proposal_id=pid, verdict="revert")
    assert out.status == "reverted"
    assert resolve_kind_slug(db, "organization") == "organization"
    assert "organization" in live_kind_slugs(db)
    assert resolve_entity_kind(db, org) == "organization"


def test_revert_add_deprecates_and_restores_entity_kinds(db: sqlite3.Connection) -> None:
    moved = _entity(db, "Attention Is All You Need", "other")
    pid = _proposal(
        db,
        action="add",
        subject_slug="research_paper",
        detail={
            "source_slug": "other",
            "label": "Research paper",
            "reassign_entity_ids": [moved],
        },
    )
    decide_correction(db, proposal_id=pid, verdict="confirm")
    assert resolve_entity_kind(db, moved) == "research_paper"

    out = decide_correction(db, proposal_id=pid, verdict="revert")
    assert out.status == "reverted"
    assert "research_paper" not in live_kind_slugs(db)
    # The moved entity went back to the kind it held before the apply —
    # via a NEWER assignment row, not by erasing anything (I2).
    assert resolve_entity_kind(db, moved) == "other"
    assert db.execute("SELECT COUNT(*) FROM entity_kind_assignments").fetchone()[0] == 2


def test_revert_split_restores_kind_and_classified_entities(db: sqlite3.Connection) -> None:
    classified = _entity(db, "Attention Is All You Need", "concept")
    straggler = _entity(db, "stoicism", "concept")
    pid = _proposal(
        db,
        action="split",
        subject_slug="concept",
        detail={
            "from_slug": "concept",
            "successors": [{"slug": "research_paper"}, {"slug": "idea"}],
            "default_slug": "idea",
            "assignments": {classified: "research_paper"},
        },
    )
    decide_correction(db, proposal_id=pid, verdict="confirm")

    out = decide_correction(db, proposal_id=pid, verdict="revert")
    assert out.status == "reverted"
    assert "concept" in live_kind_slugs(db)
    assert resolve_entity_kind(db, straggler) == "concept"
    assert resolve_entity_kind(db, classified) == "concept"


def test_revert_requires_an_applied_proposal(db: sqlite3.Connection) -> None:
    pid = _proposal(db, action="deprecate", subject_slug="place", detail={"slug": "place"})
    out = decide_correction(db, proposal_id=pid, verdict="revert")
    assert out.status == "not_applied"
    assert "place" in live_kind_slugs(db)
    assert _queue_row(db, pid)["status"] == "proposed"


# ── guardrails ───────────────────────────────────────────────────────────────


def test_other_is_protected_from_revision(db: sqlite3.Connection) -> None:
    """'other' is the write path's normalization fallback — it can never be
    deprecated, merged away, renamed, or split."""
    for action, detail in (
        ("deprecate", {"slug": "other"}),
        ("merge", {"from_slug": "other", "to_slug": "concept"}),
        ("rename", {"from_slug": "other", "to_slug": "misc"}),
        ("split", {"from_slug": "other", "successors": [{"slug": "misc"}]}),
    ):
        pid = _proposal(db, action=action, subject_slug="other", detail=detail)
        with pytest.raises(ValueError, match="normalization"):
            decide_correction(db, proposal_id=pid, verdict="confirm")
        # The refused proposal stays open — nothing half-applied.
        assert _queue_row(db, pid)["status"] == "proposed"
    assert "other" in live_kind_slugs(db)


def test_malformed_proposals_are_refused_loudly(db: sqlite3.Connection) -> None:
    # rename without a target
    pid = _proposal(db, action="rename", subject_slug="place", detail={"from_slug": "place"})
    with pytest.raises(ValueError, match="to_slug"):
        decide_correction(db, proposal_id=pid, verdict="confirm")
    # add with a slug that fails the format gate
    pid2 = _proposal(db, action="add", subject_slug="Bad Slug!", detail={"source_slug": "other"})
    with pytest.raises(ValueError, match="invalid kind slug"):
        decide_correction(db, proposal_id=pid2, verdict="confirm")
    # verdict vocabulary: 'retract' is entity-only
    pid3 = _proposal(db, action="deprecate", subject_slug="place", detail={"slug": "place"})
    with pytest.raises(ValueError, match="ontology"):
        decide_correction(db, proposal_id=pid3, verdict="retract")


# ── dispatch: ontology + entity decisions coexist on one verb ────────────────


def test_ontology_and_entity_decisions_coexist(db: sqlite3.Connection, tmp_path: Path) -> None:
    """The ont_ prefix routes to the ontology queue; plain ULIDs keep hitting
    the entity-audit path unchanged (no regression to ADR-0002 decisions)."""
    from afair.agents.entity_audit import EntityAuditWorker
    from afair.settings import Settings
    from afair.substrate import read_pending_corrections

    # Seed an entity-audit retype proposal the established way.
    person = _entity(db, "maxime.team", "person")
    EntityAuditWorker().run(
        db,
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            environment="local",
            vault_dir=tmp_path,
            cold_path_enabled=False,
        ),
    )
    retype = next(p for p in read_pending_corrections(db) if p.kind == "retype")
    # And one ontology proposal.
    ont_pid = _proposal(db, action="deprecate", subject_slug="place", detail={"slug": "place"})

    # Entity decision still works through the same function.
    ent_out = decide_correction(db, proposal_id=retype.id, verdict="confirm")
    assert ent_out.status == "applied"
    assert resolve_entity_kind(db, person) == "product"
    # Ontology decision works beside it.
    ont_out = decide_correction(db, proposal_id=ont_pid, verdict="confirm")
    assert ont_out.status == "applied"
    assert "place" not in live_kind_slugs(db)
    # 'revert' stays ontology-only: the entity path rejects it loudly.
    with pytest.raises(ValueError, match="verdict must be"):
        decide_correction(db, proposal_id=retype.id, verdict="revert")


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
    try:
        yield server_ctx
    finally:
        conn.close()


def test_recall_stats_surfaces_ontology_proposals(ctx: object) -> None:
    from afair.mcp import handlers

    db = ctx.db  # type: ignore[attr-defined]
    _proposal(
        db,
        action="add",
        subject_slug="research_paper",
        detail={"source_slug": "other", "label": "Research paper"},
    )
    result = handlers.recall(stats=True)
    views = [p for p in result.pending_corrections if p.kind == "ontology_add"]
    assert len(views) == 1
    assert views[0].subject_slug == "research_paper"
    assert views[0].id.startswith(ONTOLOGY_PROPOSAL_ID_PREFIX)
    assert "research_paper" in views[0].prompt


def test_recall_decide_applies_ontology_proposal_and_reports(ctx: object) -> None:
    from afair.mcp import handlers

    db = ctx.db  # type: ignore[attr-defined]
    pid = _proposal(db, action="deprecate", subject_slug="place", detail={"slug": "place"})

    result = handlers.recall(decide=CorrectionDecision(proposal_id=pid, verdict="confirm"))
    assert result.note is not None
    assert "ontology revision confirm" in result.note
    assert "place" not in live_kind_slugs(db)
    # The decided proposal is gone from the queue echoed back.
    assert all(p.id != pid for p in result.pending_corrections)

    # And the revert verdict rides the same wire shape.
    reverted = handlers.recall(decide=CorrectionDecision(proposal_id=pid, verdict="revert"))
    assert reverted.note is not None
    assert "ontology revision revert" in reverted.note
    assert "place" in live_kind_slugs(db)


# ── session-start resource surfacing ─────────────────────────────────────────


def test_session_start_surfaces_ontology_proposals(db: sqlite3.Connection) -> None:
    from afair.mcp.resources import build_session_start_payload

    _proposal(
        db,
        action="merge",
        subject_slug="product",
        detail={"from_slug": "product", "to_slug": "project"},
    )
    payload = build_session_start_payload(db)
    ont = [p for p in payload["pending_corrections"] if p["kind"] == "ontology_merge"]
    assert len(ont) == 1
    assert ont[0]["id"].startswith(ONTOLOGY_PROPOSAL_ID_PREFIX)
    assert "product" in ont[0]["prompt"]
    assert "ontology_" in payload["instructions"]
    assert "revert" in payload["instructions"]
