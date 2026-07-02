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
    count_pending_corrections,
    decide_correction,
    open_db,
    read_pending_corrections,
    resolve_canonical,
    resolve_entity_kind,
    write_entity,
    write_entity_merge,
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
    """Realistic seed: a person type-mismatch (retype) + a cross-kind auto-merge
    (merge_review) — the two shapes the audit catches on the live vault."""
    _entity(conn, "maxime.team", "person")  # → propose retype to product
    # Clario: project merged into product by the deduplicator → merge_review.
    from_id = _entity(conn, "Clario", "project")
    into_id = _entity(conn, "Clario", "product")
    write_entity_merge(
        conn,
        from_entity_id=from_id,
        into_entity_id=into_id,
        merged_by="entity_deduplicator:v0",
        reason="t",
        confidence=0.95,
    )
    EntityAuditWorker().run(conn, settings)


# ── read the queue ───────────────────────────────────────────────────────────


def test_read_pending_surfaces_open_proposals(db: sqlite3.Connection, settings: Settings) -> None:
    _seed_proposals(db, settings)
    pending = read_pending_corrections(db)
    kinds = sorted(p.kind for p in pending)
    assert kinds == ["merge_review", "retype"]


def test_count_pending_matches_open_queue(db: sqlite3.Connection, settings: Settings) -> None:
    """The cheap COUNT(*) companion tracks the true open total: 0 on a fresh
    vault, 2 after the seed (retype + merge_review), 1 once one is decided."""
    assert count_pending_corrections(db) == 0  # fresh, no seed
    _seed_proposals(db, settings)
    assert count_pending_corrections(db) == 2
    retype = next(p for p in read_pending_corrections(db) if p.kind == "retype")
    decide_correction(db, proposal_id=retype.id, verdict="confirm")
    assert count_pending_corrections(db) == 1


def test_pending_prompt_for_merge_review_describes_the_auto_merge(
    db: sqlite3.Connection, settings: Settings
) -> None:
    _seed_proposals(db, settings)
    mr = next(p for p in read_pending_corrections(db) if p.kind == "merge_review")
    assert "Clario" in mr.prompt
    assert "product" in mr.prompt  # the kind the merge picked
    assert "project" in mr.prompt  # the kind it came from


# ── decide: confirm applies through the append-only path ─────────────────────


def test_confirm_retype_retypes_the_entity(db: sqlite3.Connection, settings: Settings) -> None:
    """ADR-0003 Phase 2: a retype is ONE entity_kind_assignments row — the
    entity's resolved kind changes while its identity stays the same. No
    merge-chain surgery."""
    _seed_proposals(db, settings)
    retype = next(p for p in read_pending_corrections(db) if p.kind == "retype")
    person_id = retype.entity_id

    out = decide_correction(db, proposal_id=retype.id, verdict="confirm")
    assert out.status == "applied"
    # Identity unchanged: still its own canonical, no merge row written.
    assert resolve_canonical(db, person_id) == person_id
    assert db.execute("SELECT COUNT(*) FROM entity_merges").fetchone()[0] == 1  # only the seed one
    # The resolved kind flipped via exactly ONE assignment row.
    assert resolve_entity_kind(db, person_id) == "product"
    rows = db.execute(
        "SELECT kind_slug, source_event_id FROM entity_kind_assignments WHERE entity_id = ?",
        (person_id,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["kind_slug"] == "product"
    # An observe event anchors the change (I7 — recorded) and the assignment
    # references it.
    n_observe = db.execute("SELECT COUNT(*) FROM events WHERE kind = 'observe'").fetchone()[0]
    assert n_observe == 1
    assert rows[0]["source_event_id"] is not None
    # The proposal is closed and no longer surfaces.
    assert all(p.id != retype.id for p in read_pending_corrections(db))


# ── decide: merge_review confirm keeps, reject+to_kind re-types ──────────────


def test_merge_review_confirm_keeps_the_kind(db: sqlite3.Connection, settings: Settings) -> None:
    _seed_proposals(db, settings)
    mr = next(p for p in read_pending_corrections(db) if p.kind == "merge_review")
    product_id = mr.detail["into_entity_id"]

    out = decide_correction(db, proposal_id=mr.id, verdict="confirm")
    assert out.status == "confirmed"
    # Nothing applied — the merged canonical stays the product it was.
    assert resolve_entity_kind(db, product_id) == "product"
    assert db.execute("SELECT COUNT(*) FROM entity_kind_assignments").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM events WHERE kind = 'observe'").fetchone()[0] == 0
    assert all(p.id != mr.id for p in read_pending_corrections(db))


def test_merge_review_reject_with_to_kind_retypes(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """ADR-0003 Phase 2: reverting the auto-picked kind is ONE assignment row
    on the merged canonical — no fresh entity, no merge cycle, no
    merge-invalidation dance."""
    _seed_proposals(db, settings)
    mr = next(p for p in read_pending_corrections(db) if p.kind == "merge_review")
    project_id = mr.entity_id  # the merge's from-side (Clario project)
    product_id = mr.detail["into_entity_id"]  # the merged canonical

    out = decide_correction(db, proposal_id=mr.id, verdict="reject", to_kind="project")
    assert out.status == "applied"
    # The merge stays live; BOTH sides resolve to the same canonical, whose
    # resolved kind is now the operator's choice.
    assert resolve_canonical(db, project_id) == product_id
    assert resolve_canonical(db, product_id) == product_id
    assert resolve_entity_kind(db, product_id) == "project"
    assert db.execute("SELECT COUNT(*) FROM entity_kind_assignments").fetchone()[0] == 1
    # No cycle-avoidance machinery fired: zero merge invalidations.
    assert db.execute("SELECT COUNT(*) FROM merge_invalidations").fetchone()[0] == 0


def test_merge_review_reject_to_third_kind_retypes_cleanly(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """Correcting to a kind that is neither side of the merge is the same
    single assignment row — everything keeps resolving to the canonical."""
    _seed_proposals(db, settings)
    mr = next(p for p in read_pending_corrections(db) if p.kind == "merge_review")
    project_id = mr.entity_id
    product_id = mr.detail["into_entity_id"]

    out = decide_correction(db, proposal_id=mr.id, verdict="reject", to_kind="concept")
    assert out.status == "applied"
    assert resolve_canonical(db, project_id) == product_id
    assert resolve_entity_kind(db, product_id) == "concept"
    assert db.execute("SELECT COUNT(*) FROM entity_kind_assignments").fetchone()[0] == 1


def test_merge_review_reject_without_to_kind_only_flags(
    db: sqlite3.Connection, settings: Settings
) -> None:
    _seed_proposals(db, settings)
    mr = next(p for p in read_pending_corrections(db) if p.kind == "merge_review")
    product_id = mr.detail["into_entity_id"]

    out = decide_correction(db, proposal_id=mr.id, verdict="reject")
    assert out.status == "rejected"
    assert resolve_entity_kind(db, product_id) == "product"  # untouched
    assert db.execute("SELECT COUNT(*) FROM entity_kind_assignments").fetchone()[0] == 0


def test_decide_rejects_bad_to_kind(db: sqlite3.Connection, settings: Settings) -> None:
    _seed_proposals(db, settings)
    mr = next(p for p in read_pending_corrections(db) if p.kind == "merge_review")
    with pytest.raises(ValueError, match="to_kind must be one of"):
        decide_correction(db, proposal_id=mr.id, verdict="reject", to_kind="banana")


def _append_kind_revision(
    conn: sqlite3.Connection, *, action: str, from_slug: str, to_slug: str | None = None
) -> None:
    """Append a raw kind_revisions row (what a Phase-5 apply path will write)."""
    from ulid import ULID

    with conn:
        conn.execute(
            """
            INSERT INTO kind_revisions (
                id, action, from_slug, to_slug, detail,
                revised_at, revised_by, reason, source_event_id
            ) VALUES (?, ?, ?, ?, NULL, ?, 'test', 'test revision', NULL)
            """,
            (str(ULID()), action, from_slug, to_slug, "2026-07-01T00:00:00+00:00"),
        )


def test_decide_rejects_dead_slug(db: sqlite3.Connection, settings: Settings) -> None:
    """to_kind validation reads the kind registry (ADR-0003 Phase 1): a
    deprecated slug is no longer a valid target, exactly like an unknown one."""
    _seed_proposals(db, settings)
    mr = next(p for p in read_pending_corrections(db) if p.kind == "merge_review")
    _append_kind_revision(db, action="deprecate", from_slug="concept")
    with pytest.raises(ValueError, match="to_kind must be one of"):
        decide_correction(db, proposal_id=mr.id, verdict="reject", to_kind="concept")


def test_decide_resolves_renamed_slug_to_live_successor(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """A slug renamed away in the registry resolves through the revision chain
    to its live successor before the retype is applied."""
    _seed_proposals(db, settings)
    mr = next(p for p in read_pending_corrections(db) if p.kind == "merge_review")
    with db:
        db.execute(
            "INSERT INTO kind_registry (id, slug, label, description, created_at, "
            "created_by, source_event_id) "
            "VALUES ('kind:company', 'company', 'Company', NULL, "
            "'2026-07-01T00:00:00+00:00', 'test', NULL)"
        )
    _append_kind_revision(db, action="rename", from_slug="organization", to_slug="company")

    out = decide_correction(db, proposal_id=mr.id, verdict="reject", to_kind="organization")
    assert out.status == "applied"
    # The assignment landed on the resolved live kind, not the dead slug.
    product_id = mr.detail["into_entity_id"]
    assert resolve_entity_kind(db, product_id) == "company"
    row = db.execute(
        "SELECT kind_slug FROM entity_kind_assignments WHERE entity_id = ?", (product_id,)
    ).fetchone()
    assert row["kind_slug"] == "company"


def test_merge_review_retract_withdraws_the_entity(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """For a noise merge ("scripts/smoke_mcp.py"), retract withdraws the merged
    canonical from the live graph instead of asking which kind."""
    from afair.substrate import retracted_entity_ids

    from_id = _entity(db, "scripts/smoke_mcp.py", "project")
    into_id = _entity(db, "scripts/smoke_mcp.py", "product")
    write_entity_merge(
        db,
        from_entity_id=from_id,
        into_entity_id=into_id,
        merged_by="entity_deduplicator:v0",
        reason="t",
        confidence=0.9,
    )
    EntityAuditWorker().run(db, settings)
    mr = next(
        p
        for p in read_pending_corrections(db)
        if p.kind == "merge_review" and p.entity_name == "scripts/smoke_mcp.py"
    )

    out = decide_correction(db, proposal_id=mr.id, verdict="retract")
    assert out.status == "applied"
    assert "retracted" in out.note
    assert into_id in retracted_entity_ids(db)
    # The proposal is closed and no longer surfaces.
    assert all(p.id != mr.id for p in read_pending_corrections(db))


# ── decide: reject closes untouched ──────────────────────────────────────────


def test_reject_closes_without_applying(db: sqlite3.Connection, settings: Settings) -> None:
    _seed_proposals(db, settings)
    retype = next(p for p in read_pending_corrections(db) if p.kind == "retype")
    person_id = retype.entity_id

    out = decide_correction(db, proposal_id=retype.id, verdict="reject")
    assert out.status == "rejected"
    # Not re-typed — kind unchanged, no assignment row.
    assert resolve_entity_kind(db, person_id) == "person"
    assert db.execute("SELECT COUNT(*) FROM entity_kind_assignments").fetchone()[0] == 0
    # No observe event written on a reject.
    assert db.execute("SELECT COUNT(*) FROM events WHERE kind = 'observe'").fetchone()[0] == 0
    assert all(p.id != retype.id for p in read_pending_corrections(db))


# ── decide: idempotency + guards ─────────────────────────────────────────────


def test_decide_twice_is_a_no_op(db: sqlite3.Connection, settings: Settings) -> None:
    _seed_proposals(db, settings)
    retype = next(p for p in read_pending_corrections(db) if p.kind == "retype")

    first = decide_correction(db, proposal_id=retype.id, verdict="confirm")
    assert first.status == "applied"
    rows_after_first = db.execute("SELECT COUNT(*) FROM entity_kind_assignments").fetchone()[0]
    assert rows_after_first == 1
    second = decide_correction(db, proposal_id=retype.id, verdict="confirm")
    assert second.status == "already_decided"
    # The second confirm applies nothing — no extra assignment from a re-run.
    rows_after_second = db.execute("SELECT COUNT(*) FROM entity_kind_assignments").fetchone()[0]
    assert rows_after_second == rows_after_first


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
    assert {p.kind for p in result.pending_corrections} == {"retype", "merge_review"}


def test_recall_without_stats_omits_pending(ctx: object) -> None:
    from afair.mcp import handlers

    result = handlers.recall(query="anything")
    assert result.pending_corrections == []


def test_plain_recall_carries_count_without_list(ctx: object) -> None:
    """The core fix: a plain query recall surfaces the TRUE open-queue total
    (the nudge signal) while the heavy list stays gated off. The pile-up was
    silent precisely because this integer did not exist."""
    from afair.mcp import handlers

    result = handlers.recall(query="anything")
    assert result.pending_corrections_count == 2  # seed: retype + merge_review
    assert result.pending_corrections == []  # list still gated behind stats/decide


def test_single_event_lookup_carries_count(ctx: object) -> None:
    """The count rides the single-event lookup returns too — both the hit path
    and the miss path — so a by_id recall nudges just like a query recall."""
    from afair.mcp import handlers

    db = ctx.db  # type: ignore[attr-defined]
    ev = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": "a note"}
    )
    hit = handlers.recall(by_id=ev.id)
    assert len(hit.hits) == 1
    assert hit.pending_corrections_count == 2
    assert hit.pending_corrections == []

    miss = handlers.recall(by_id="nope")
    assert miss.hits == []
    assert miss.pending_corrections_count == 2


def test_recall_decide_count_reflects_remaining(ctx: object) -> None:
    """The count is computed post-decide: a just-confirmed proposal is already
    excluded, so the echoed count drops to the remaining total."""
    from afair.mcp import handlers

    retype = next(p for p in handlers.recall(stats=True).pending_corrections if p.kind == "retype")
    result = handlers.recall(decide=CorrectionDecision(proposal_id=retype.id, verdict="confirm"))
    assert result.pending_corrections_count == 1


def test_recall_count_sums_both_queues(ctx: object) -> None:
    """The nudge total covers BOTH queues `_pending_correction_views` merges:
    the seed's 2 entity-audit proposals plus one Schema-Evolver ontology
    proposal → 3, so the number never contradicts the list the client fetches
    next."""
    import json

    from ulid import ULID

    from afair.mcp import handlers
    from afair.substrate.kinds import ONTOLOGY_PROPOSAL_ID_PREFIX

    db = ctx.db  # type: ignore[attr-defined]
    with db:
        db.execute(
            """
            INSERT INTO proposed_ontology_revisions (
                id, action, subject_slug, detail, evidence, confidence,
                detected_by, detected_at, status
            ) VALUES (?, 'add', 'research_paper', ?, 'test signal', 0.8,
                      'schema_evolver:v0', '2026-07-01T00:00:00+00:00', 'proposed')
            """,
            (
                f"{ONTOLOGY_PROPOSAL_ID_PREFIX}{ULID()!s}",
                json.dumps({"source_slug": "other", "label": "Research paper"}),
            ),
        )

    result = handlers.recall(query="anything")
    assert result.pending_corrections_count == 3  # 2 entity-audit + 1 ontology


def test_recall_decide_applies_and_reports(ctx: object) -> None:
    from afair.mcp import handlers

    proposals = handlers.recall(stats=True).pending_corrections
    retype = next(p for p in proposals if p.kind == "retype")

    result = handlers.recall(decide=CorrectionDecision(proposal_id=retype.id, verdict="confirm"))
    assert result.note is not None
    assert "confirm" in result.note
    db = ctx.db  # type: ignore[attr-defined]
    # One assignment row; identity unchanged; the resolved kind flipped.
    assert resolve_canonical(db, retype.entity_id) == retype.entity_id
    assert resolve_entity_kind(db, retype.entity_id) == "product"
    # The decided proposal is gone from the remaining queue echoed back.
    assert all(p.id != retype.id for p in result.pending_corrections)


def test_recall_overlay_drops_retracted_entity(ctx: object) -> None:
    """A retracted entity disappears from the recall entity overlay even though
    its row + mention remain (I2)."""
    from afair.mcp import handlers
    from afair.substrate import (
        read_event_by_id,
        retract_entity,
        write_entity,
        write_entity_mention,
        write_event,
    )

    db = ctx.db  # type: ignore[attr-defined]
    ev = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": "a note"}
    )
    e = write_entity(
        db,
        canonical_name="scripts/x.py",
        kind="product",
        created_by="t",
        source_event_id=ev.id,
        confidence=0.8,
    )
    write_entity_mention(
        db,
        entity_id=e.id,
        event_id=ev.id,
        event_hash=ev.content_hash,
        surface_form="scripts/x.py",
        canonicalized_by="t",
        match_method="new",
        confidence=0.8,
    )
    event = read_event_by_id(db, ev.id)
    assert event is not None

    before = handlers._build_entity_overlay([event], db)
    names = [x["canonical_name"] for x in before[ev.content_hash]["canonical_entities"]]
    assert "scripts/x.py" in names

    retract_entity(db, entity_id=e.id, retracted_by="operator", reason="noise")
    after = handlers._build_entity_overlay([event], db)
    # The whole overlay entry is gone (it was the only entity on the event).
    assert ev.content_hash not in after or not after[ev.content_hash].get("canonical_entities")


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
