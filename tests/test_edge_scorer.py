"""EdgeConfidenceScorer cold-path worker tests (ADR-0004 S4).

Exercises backfill of legacy flat-0.8 edges, graceful degradation when signals
are unrecoverable, re-scoring on new corroboration / a contested source,
idempotency, the per-cycle cap, and the calibration_report mechanics.

The scorer uses no LLM, so these run against real SQLite with no mocking.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from afair.agents import edge_scorer as es
from afair.agents.edge_scorer import EdgeConfidenceScorer
from afair.agents.interpretation import write_interpretation
from afair.settings import Settings
from afair.substrate import (
    latest_edge_scores_batch,
    open_db,
    record_edge_review,
    record_edge_serves,
    write_entity,
    write_entity_edge,
    write_entity_mention,
    write_event,
)
from afair.substrate.confidence import (
    EDGE_CONFIDENCE_VERSION,
    EdgeConfidenceSignals,
    calibration_report,
    compute_edge_confidence,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path

    from afair.substrate.entities import Entity, EntityEdge
    from afair.substrate.events import Event


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


def _entity(conn: sqlite3.Connection, name: str, kind: str, ev: Event) -> Entity:
    return write_entity(
        conn,
        canonical_name=name,
        kind=kind,
        created_by="test",
        source_event_id=ev.id,
        confidence=0.9,
    )


def _seed_edge(
    conn: sqlite3.Connection,
    *,
    text: str = "Sajinth runs Athara",
    predicate: str = "runs",
    extraction_confidence: float | None = 0.9,
    subj_conf: float | None = 1.0,
    obj_conf: float | None = 1.0,
    edge_confidence: float = 0.8,
    with_interpretation: bool = True,
    subj: Entity | None = None,
    obj: Entity | None = None,
    subject_name: str = "Sajinth",
    object_name: str = "Athara",
) -> tuple[Event, Entity, Entity, EntityEdge]:
    """Write event (+ optional extractor interpretation), two entities, their
    mentions, and one edge. Mirrors the substrate a real cycle would produce."""
    ev = write_event(
        conn, origin="user", kind="remember", payload={"content_type": "text", "text": text}
    )
    if with_interpretation:
        extraction: dict[str, Any] = {
            "status": "success",
            "best_guess_kind": "fact",
            "summary": text,
        }
        if extraction_confidence is not None:
            extraction["confidence"] = extraction_confidence
        write_interpretation(
            conn,
            event=ev,
            version=1,
            produced_by="extractor:anthropic/claude-haiku-4-5",
            extraction=extraction,
        )
    subj = subj or _entity(conn, subject_name, "person", ev)
    obj = obj or _entity(conn, object_name, "organization", ev)
    if subj_conf is not None:
        write_entity_mention(
            conn,
            entity_id=subj.id,
            event_id=ev.id,
            event_hash=ev.content_hash,
            surface_form=subj.canonical_name,
            canonicalized_by="test",
            match_method="exact",
            confidence=subj_conf,
        )
    if obj_conf is not None:
        write_entity_mention(
            conn,
            entity_id=obj.id,
            event_id=ev.id,
            event_hash=ev.content_hash,
            surface_form=obj.canonical_name,
            canonicalized_by="test",
            match_method="exact",
            confidence=obj_conf,
        )
    edge = write_entity_edge(
        conn,
        subject_id=subj.id,
        predicate=predicate,
        object_id=obj.id,
        source_event_id=ev.id,
        discovered_by="test",
        confidence=edge_confidence,
    )
    assert edge is not None
    return ev, subj, obj, edge


def _write_conflict(conn: sqlite3.Connection, source_event: Event) -> None:
    """Write a conflict-resolver verdict interpretation flagging the source
    event as in unresolved conflict with some other event."""
    other_hash = "sha256:" + "b" * 64
    write_interpretation(
        conn,
        event=source_event,
        version=1,
        produced_by=f"conflict_resolver:v0:{other_hash}",
        extraction={
            "verdict": "conflicts",
            "event_a_hash": source_event.content_hash,
            "event_b_hash": other_hash,
            "event_b_id": "evt_other",
            "confidence": 0.9,
            "reason": "genuine disagreement",
        },
    )


# ── backfill + recovery ─────────────────────────────────────────────────────


def test_legacy_flat_edge_gets_backfilled_score(db: sqlite3.Connection, settings: Settings) -> None:
    _ev, _subj, _obj, edge = _seed_edge(db, edge_confidence=0.8)
    stats = EdgeConfidenceScorer().run(db, settings)
    assert stats["edges_scored"] == 1
    assert stats["legacy_backfilled"] == 1

    scores = latest_edge_scores_batch(db, [edge.id])
    assert edge.id in scores
    score = scores[edge.id]
    assert score.computed_by == EDGE_CONFIDENCE_VERSION
    expected, _ = compute_edge_confidence(
        EdgeConfidenceSignals(
            extraction_confidence=0.9,
            subject_mention_confidence=1.0,
            object_mention_confidence=1.0,
            predicate="runs",
            corroborating_sources=0,
            source_conflicted=False,
        )
    )
    assert abs(score.confidence - expected) < 1e-9


def test_unrecoverable_signals_degrade_gracefully(
    db: sqlite3.Connection, settings: Settings
) -> None:
    # No extractor interpretation, no mentions → signals None, but the score
    # row is still written (crispness-only).
    _ev, _subj, _obj, edge = _seed_edge(
        db, with_interpretation=False, subj_conf=None, obj_conf=None
    )
    stats = EdgeConfidenceScorer().run(db, settings)
    assert stats["edges_scored"] == 1
    score = latest_edge_scores_batch(db, [edge.id])[edge.id]
    signals = score.components["signals"]
    assert signals["extraction_confidence"] is None
    assert signals["subject_mention_confidence"] is None
    assert signals["object_mention_confidence"] is None


def test_rescore_on_new_corroboration(db: sqlite3.Connection, settings: Settings) -> None:
    _ev1, subj, obj, edge1 = _seed_edge(db)
    EdgeConfidenceScorer().run(db, settings)
    first = latest_edge_scores_batch(db, [edge1.id])[edge1.id].confidence

    # A second event re-asserts the SAME triple (both endpoints reused).
    _ev2, _s, _o, _edge2 = _seed_edge(db, text="Sajinth runs Athara again", subj=subj, obj=obj)
    EdgeConfidenceScorer().run(db, settings)

    # edge1 re-scored higher (now corroborated by edge2's source event).
    rescored = latest_edge_scores_batch(db, [edge1.id])[edge1.id].confidence
    assert rescored > first
    comp = latest_edge_scores_batch(db, [edge1.id])[edge1.id].components
    assert comp["signals"]["corroborating_sources"] == 1


def test_contested_source_lowers_confidence(db: sqlite3.Connection, settings: Settings) -> None:
    ev, _subj, _obj, edge = _seed_edge(db)
    EdgeConfidenceScorer().run(db, settings)
    before = latest_edge_scores_batch(db, [edge.id])[edge.id].confidence

    _write_conflict(db, ev)
    EdgeConfidenceScorer().run(db, settings)
    after = latest_edge_scores_batch(db, [edge.id])[edge.id].confidence
    assert after < before
    comp = latest_edge_scores_batch(db, [edge.id])[edge.id].components
    assert comp["signals"]["source_conflicted"] is True


def test_idempotent_cycles(db: sqlite3.Connection, settings: Settings) -> None:
    _ev, _subj, _obj, _edge = _seed_edge(db)
    EdgeConfidenceScorer().run(db, settings)
    n1 = db.execute("SELECT COUNT(*) AS c FROM edge_confidence_scores").fetchone()["c"]
    stats2 = EdgeConfidenceScorer().run(db, settings)
    n2 = db.execute("SELECT COUNT(*) AS c FROM edge_confidence_scores").fetchone()["c"]
    assert n2 == n1  # no new rows on the second, unchanged cycle
    assert stats2["edges_scored"] == 0
    assert stats2["edges_skipped_unchanged"] == 1


def test_bounded_per_cycle(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(es, "MAX_EDGES_PER_CYCLE", 2)
    for i in range(5):
        _seed_edge(db, text=f"Person{i} leads Org{i}", predicate="leads")
    stats = EdgeConfidenceScorer().run(db, settings)
    assert stats["edges_scored"] <= 2


# ── calibration_report ──────────────────────────────────────────────────────


def test_calibration_report_empty_is_insufficient(db: sqlite3.Connection) -> None:
    report = calibration_report(db)
    assert report["reviewed"] == 0
    assert report["sufficient"] is False
    assert report["brier"] is None
    assert report["buckets"] == []


def test_calibration_report_bucket_and_brier_math(
    db: sqlite3.Connection, settings: Settings
) -> None:
    # Three edges, each with a known served confidence and a known verdict.
    # Build them, score them, then override the served score to a fixed value
    # by writing a v1 score directly is awkward — instead assert against the
    # ACTUAL served confidence the scorer computes.
    from afair.substrate import latest_edge_confidence_batch, write_edge_confidence_score

    edges = []
    for i in range(3):
        _ev, _subj, _obj, edge = _seed_edge(db, text=f"A{i} runs B{i}", subj=None, obj=None)
        edges.append(edge)

    # Give each edge a fixed, distinct served confidence via a direct v1 row.
    fixed = {edges[0].id: 0.9, edges[1].id: 0.8, edges[2].id: 0.2}
    for eid, conf in fixed.items():
        write_edge_confidence_score(
            db, edge_id=eid, confidence=conf, components={}, computed_by=EDGE_CONFIDENCE_VERSION
        )
    served = latest_edge_confidence_batch(db, [e.id for e in edges])
    assert served == fixed

    # Two confirms (0.9, 0.8) + one reject (0.2).
    record_edge_review(db, edge_id=edges[0].id, verdict="confirm", reviewed_by="op")
    record_edge_review(db, edge_id=edges[1].id, verdict="confirm", reviewed_by="op")
    record_edge_review(db, edge_id=edges[2].id, verdict="reject", reviewed_by="op")

    report = calibration_report(db)
    assert report["reviewed"] == 3
    assert report["confirmed"] == 2
    assert report["rejected"] == 1
    # Not enough labels for sufficiency (needs >= 20, >= 5 each class).
    assert report["sufficient"] is False
    # Brier = mean[(0.9-1)^2, (0.8-1)^2, (0.2-0)^2] = (0.01 + 0.04 + 0.04)/3.
    expected_brier = (0.01 + 0.04 + 0.04) / 3
    assert report["brier"] is not None
    assert abs(report["brier"] - expected_brier) < 1e-9
    # Bucket [0.75, 1.0] holds the two confirms (confirm_rate 1.0); the reject
    # sits in [0.0, 0.25] (confirm_rate 0.0).
    top = next(b for b in report["buckets"] if b["lo"] == 0.75)
    assert top["n"] == 2
    assert top["confirm_rate"] == 1.0
    low = next(b for b in report["buckets"] if b["lo"] == 0.0)
    assert low["n"] == 1
    assert low["confirm_rate"] == 0.0


# ── edge-review proposals + decide loop (ADR-0004 S6) ───────────────────────


def _seed_weak_edge(conn: sqlite3.Connection, i: int, *, serve: bool = True) -> Any:
    """A low-confidence edge (vague predicate, new endpoints, no extraction) on
    a DISTINCT subject so the UNIQUE(kind, subject) doesn't collapse proposals.
    Its computed served confidence is ~0.365 — below both the 0.5 expiry floor
    and the 0.6 review threshold.

    Stamped as SERVED by default — the serve-gated review query only proposes
    edges recall actually surfaced. Pass ``serve=False`` to model an edge recall
    never returned (a candidate for auto-expiry)."""
    _ev, _subj, _obj, edge = _seed_edge(
        conn,
        text=f"Person{i} is loosely connected to the Org{i}",
        predicate="is loosely connected to the",
        extraction_confidence=None,
        with_interpretation=False,
        subj_conf=0.5,
        obj_conf=0.5,
        subject_name=f"Person{i}",
        object_name=f"Org{i}",
    )
    if serve:
        record_edge_serves(conn, [edge.id])
    return edge


def test_scorer_proposes_at_most_k_lowest_confidence(
    db: sqlite3.Connection, settings: Settings
) -> None:
    from afair.substrate import read_pending_corrections

    for i in range(5):
        _seed_weak_edge(db, i)
    stats = EdgeConfidenceScorer().run(db, settings)
    assert stats["edge_reviews_proposed"] == 3  # capped at MAX_..._PER_CYCLE

    pending = read_pending_corrections(db)
    edge_reviews = [p for p in pending if p.kind == "edge_review"]
    assert len(edge_reviews) == 3
    # Each carries a ready-to-ask prompt and a reason string.
    assert all("Is that relation right?" in p.prompt for p in edge_reviews)
    assert all("served confidence" in p.evidence for p in edge_reviews)


def test_scorer_skips_reviewed_invalidated_and_confident(
    db: sqlite3.Connection, settings: Settings
) -> None:
    from afair.substrate import read_pending_corrections

    # A strong edge → never proposed.
    _seed_edge(db, subject_name="Strong", object_name="Case")
    # A weak edge already reviewed → skipped.
    reviewed_edge = _seed_weak_edge(db, 1)
    record_edge_review(db, edge_id=reviewed_edge.id, verdict="confirm", reviewed_by="op")
    # A weak edge that will be proposed.
    _seed_weak_edge(db, 2)

    EdgeConfidenceScorer().run(db, settings)
    edge_reviews = [p for p in read_pending_corrections(db) if p.kind == "edge_review"]
    # Only the single un-reviewed weak edge (Person2) is proposed.
    assert len(edge_reviews) == 1
    assert edge_reviews[0].detail["subject_name"] == "Person2"


def test_proposals_idempotent_across_cycles(db: sqlite3.Connection, settings: Settings) -> None:
    _seed_weak_edge(db, 1)
    EdgeConfidenceScorer().run(db, settings)
    n1 = db.execute(
        "SELECT COUNT(*) AS c FROM proposed_corrections WHERE kind = 'edge_review'"
    ).fetchone()["c"]
    EdgeConfidenceScorer().run(db, settings)
    n2 = db.execute(
        "SELECT COUNT(*) AS c FROM proposed_corrections WHERE kind = 'edge_review'"
    ).fetchone()["c"]
    assert n1 == 1
    assert n2 == 1  # INSERT OR IGNORE on the UNIQUE — no duplicate


def test_calibration_growth_resumes_after_decide_same_subject(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """P1-1: with the partial unique index (one OPEN row per subject), deciding
    a subject's queued edge_review frees the slot so its NEXT sub-threshold edge
    can queue — calibration growth resumes on decide, not on prune. The decided
    edge itself never re-proposes (its guard is the append-only edge_reviews)."""
    from afair.substrate import decide_correction, latest_edge_review, read_pending_corrections

    ev = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": "X seed"}
    )
    subj_x = _entity(db, "Xsubject", "person", ev)

    def _weak_edge_on_x(obj_name: str) -> Any:
        _e, _s, _o, edge = _seed_edge(
            db,
            text=f"Xsubject is loosely connected to the {obj_name}",
            predicate="is loosely connected to the",
            extraction_confidence=None,
            with_interpretation=False,
            subj=subj_x,
            subj_conf=0.5,
            obj_conf=0.5,
            object_name=obj_name,
        )
        record_edge_serves(db, [edge.id])
        return edge

    edge_a = _weak_edge_on_x("OrgAlpha")
    edge_b = _weak_edge_on_x("OrgBeta")

    # Cycle 1: one OPEN proposal for subject X (the partial index absorbs the
    # second edge until the first is decided).
    EdgeConfidenceScorer().run(db, settings)
    open_reviews = [p for p in read_pending_corrections(db) if p.kind == "edge_review"]
    assert len(open_reviews) == 1
    first_edge_id = open_reviews[0].detail["edge_id"]

    # Decide it → edge_reviews row written, proposal closed, slot freed.
    decide_correction(db, proposal_id=open_reviews[0].id, verdict="confirm")
    assert latest_edge_review(db, first_edge_id) == "confirm"

    # Cycle 2: the OTHER sub-threshold edge of X now queues (growth resumes);
    # the decided edge never re-proposes (edge_reviews NOT EXISTS).
    EdgeConfidenceScorer().run(db, settings)
    open_reviews_2 = [p for p in read_pending_corrections(db) if p.kind == "edge_review"]
    assert len(open_reviews_2) == 1
    second_edge_id = open_reviews_2[0].detail["edge_id"]
    assert second_edge_id != first_edge_id
    assert {first_edge_id, second_edge_id} == {edge_a.id, edge_b.id}


def test_decide_confirm_records_review_end_to_end(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """REGRESSION: propose → decide(confirm) → a confirm review row exists.
    This full loop cannot pass on main — nothing proposes edge reviews, so
    record_edge_review never gets a production caller."""
    from afair.substrate import decide_correction, latest_edge_review, read_pending_corrections

    edge = _seed_weak_edge(db, 1)
    EdgeConfidenceScorer().run(db, settings)
    proposal = next(p for p in read_pending_corrections(db) if p.kind == "edge_review")

    outcome = decide_correction(db, proposal_id=proposal.id, verdict="confirm")
    assert outcome.status == "applied"
    assert latest_edge_review(db, edge.id) == "confirm"
    # The proposal is closed; re-deciding is a no-op.
    again = decide_correction(db, proposal_id=proposal.id, verdict="confirm")
    assert again.status == "already_decided"


def test_decide_reject_drops_edge_from_graph(db: sqlite3.Connection, settings: Settings) -> None:
    from afair.substrate import (
        decide_correction,
        iter_edges_for_entity,
        latest_edge_review,
        read_pending_corrections,
    )

    edge = _seed_weak_edge(db, 1)
    EdgeConfidenceScorer().run(db, settings)
    proposal = next(p for p in read_pending_corrections(db) if p.kind == "edge_review")

    outcome = decide_correction(db, proposal_id=proposal.id, verdict="reject")
    assert outcome.status == "applied"
    assert latest_edge_review(db, edge.id) == "reject"
    # The reject wrote an invalidation → the edge is gone from live reads.
    live = iter_edges_for_entity(db, edge.subject_id)
    assert all(e.id != edge.id for e in live)


def test_decide_retract_on_edge_raises(db: sqlite3.Connection, settings: Settings) -> None:
    from afair.substrate import decide_correction, read_pending_corrections

    _seed_weak_edge(db, 1)
    EdgeConfidenceScorer().run(db, settings)
    proposal = next(p for p in read_pending_corrections(db) if p.kind == "edge_review")
    with pytest.raises(ValueError, match="retract is not meaningful"):
        decide_correction(db, proposal_id=proposal.id, verdict="retract")


def test_decide_stale_edge_id_closes_proposal(db: sqlite3.Connection, settings: Settings) -> None:
    from afair.substrate import decide_correction, read_pending_corrections

    _seed_weak_edge(db, 1)
    EdgeConfidenceScorer().run(db, settings)
    proposal = next(p for p in read_pending_corrections(db) if p.kind == "edge_review")
    # Corrupt the stored edge_id to simulate a stale reference.
    detail = dict(proposal.detail)
    detail["edge_id"] = "edge:ghost"
    import json as _json

    db.execute(
        "UPDATE proposed_corrections SET detail = ? WHERE id = ?",
        (_json.dumps(detail), proposal.id),
    )
    db.commit()
    outcome = decide_correction(db, proposal_id=proposal.id, verdict="confirm")
    assert outcome.status == "not_found"
    # The proposal is closed (rejected) so it stops blocking the queue.
    remaining = [p for p in read_pending_corrections(db) if p.id == proposal.id]
    assert remaining == []


# ── serve-gated review + auto-expiry of never-served low-confidence edges ────


def test_record_edge_serves_is_idempotent(db: sqlite3.Connection) -> None:
    edge = _seed_weak_edge(db, 1, serve=False)
    first = record_edge_serves(db, [edge.id])
    assert first == 1  # one new stamp
    again = record_edge_serves(db, [edge.id, edge.id])
    assert again == 0  # already stamped; INSERT OR IGNORE absorbs it
    rows = db.execute(
        "SELECT first_served_at FROM edge_serves WHERE edge_id = ?", (edge.id,)
    ).fetchall()
    assert len(rows) == 1  # exactly one row, timestamp never moved


def test_edge_serves_is_append_only(db: sqlite3.Connection) -> None:
    """I2: edge_serves triggers refuse UPDATE and DELETE."""
    import pytest as _pytest

    edge = _seed_weak_edge(db, 1, serve=False)
    record_edge_serves(db, [edge.id])
    with _pytest.raises(Exception, match="append-only"):
        db.execute("UPDATE edge_serves SET first_served_at = 'x' WHERE edge_id = ?", (edge.id,))
    with _pytest.raises(Exception, match="append-only"):
        db.execute("DELETE FROM edge_serves WHERE edge_id = ?", (edge.id,))


def test_served_low_conf_is_queued_unserved_same_conf_is_not(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """REGRESSION: identical sub-0.6 edges — the SERVED one is queued for
    review, the never-served one is not. Both fresh, so neither is expired."""
    from afair.substrate import read_pending_corrections

    served = _seed_weak_edge(db, 1, serve=True)
    unserved = _seed_weak_edge(db, 2, serve=False)

    EdgeConfidenceScorer().run(db, settings)

    reviews = [p for p in read_pending_corrections(db) if p.kind == "edge_review"]
    edge_ids = {p.detail["edge_id"] for p in reviews}
    assert served.id in edge_ids
    assert unserved.id not in edge_ids


def test_never_served_low_conf_old_edge_is_auto_expired(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REGRESSION: a never-served <0.5 edge past the grace period is invalidated
    (auto-expired) and writes NO edge_review row — the calibration set stays a
    pure record of operator verdicts."""
    from afair.substrate import iter_edges_for_entity, read_pending_corrections

    monkeypatch.setattr(es, "EDGE_EXPIRY_MIN_AGE_DAYS", -1)  # treat the fresh edge as old
    edge = _seed_weak_edge(db, 1, serve=False)

    stats = EdgeConfidenceScorer().run(db, settings)
    assert stats["edges_expired_unserved"] == 1
    # Invalidated → gone from live reads.
    live = iter_edges_for_entity(db, edge.subject_id)
    assert all(e.id != edge.id for e in live)
    # No edge_review row was written (auto-expiry must not contaminate calibration).
    assert db.execute("SELECT COUNT(*) FROM edge_reviews").fetchone()[0] == 0
    # No edge_review PROPOSAL was queued for it either.
    reviews = [p for p in read_pending_corrections(db) if p.kind == "edge_review"]
    assert all(p.detail["edge_id"] != edge.id for p in reviews)
    # The invalidation is producer-tagged + reasoned (I7).
    inval = db.execute(
        "SELECT invalidated_by, reason FROM edge_invalidations WHERE edge_id = ?", (edge.id,)
    ).fetchone()
    assert inval["invalidated_by"] == es.EDGE_AUTO_EXPIRE_PRODUCER
    assert "never served" in inval["reason"]


def test_fresh_never_served_edge_is_not_expired(db: sqlite3.Connection, settings: Settings) -> None:
    """The 14-day grace holds: a never-served low-conf edge younger than the
    grace is left alone (recall still has a chance to surface it)."""
    edge = _seed_weak_edge(db, 1, serve=False)
    stats = EdgeConfidenceScorer().run(db, settings)
    assert stats["edges_expired_unserved"] == 0
    assert (
        db.execute(
            "SELECT COUNT(*) FROM edge_invalidations WHERE edge_id = ?", (edge.id,)
        ).fetchone()[0]
        == 0
    )


def test_reviewed_edge_is_never_auto_expired(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An edge the operator touched (a review row) is entrenched (ADR-0002) and
    never auto-expired, even if never served + old + low confidence."""
    monkeypatch.setattr(es, "EDGE_EXPIRY_MIN_AGE_DAYS", -1)
    edge = _seed_weak_edge(db, 1, serve=False)
    record_edge_review(db, edge_id=edge.id, verdict="confirm", reviewed_by="op")

    stats = EdgeConfidenceScorer().run(db, settings)
    assert stats["edges_expired_unserved"] == 0
    assert (
        db.execute(
            "SELECT COUNT(*) FROM edge_invalidations WHERE edge_id = ?", (edge.id,)
        ).fetchone()[0]
        == 0
    )


def test_auto_expiry_is_capped_and_drains(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At most MAX_EDGE_EXPIRIES_PER_CYCLE per run; the backlog drains across
    cycles (an invalidated edge leaves the candidate set, so it is chosen once)."""
    monkeypatch.setattr(es, "EDGE_EXPIRY_MIN_AGE_DAYS", -1)
    monkeypatch.setattr(es, "MAX_EDGE_EXPIRIES_PER_CYCLE", 5)
    for i in range(7):
        _seed_weak_edge(db, i, serve=False)

    first = EdgeConfidenceScorer().run(db, settings)
    assert first["edges_expired_unserved"] == 5  # capped
    second = EdgeConfidenceScorer().run(db, settings)
    assert second["edges_expired_unserved"] == 2  # the remaining two drain
    third = EdgeConfidenceScorer().run(db, settings)
    assert third["edges_expired_unserved"] == 0  # nothing left


def test_auto_expiry_leaves_calibration_report_pure(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auto-expiry writes no edge_reviews, so calibration still sees zero
    reviewed edges (it only counts operator verdicts)."""
    from afair.substrate.confidence import calibration_report

    monkeypatch.setattr(es, "EDGE_EXPIRY_MIN_AGE_DAYS", -1)
    for i in range(3):
        _seed_weak_edge(db, i, serve=False)
    EdgeConfidenceScorer().run(db, settings)
    assert calibration_report(db)["reviewed"] == 0


# ── proposed_corrections CHECK migration (ADR-0004 S6a) ─────────────────────


def test_migrate_widens_kind_check_preserving_rows(db: sqlite3.Connection) -> None:
    """A pre-ADR-0004 vault whose frozen CHECK lacks 'edge_review' is migrated
    in place: existing rows survive and edge_review inserts start working."""
    import json as _json

    from afair.substrate.schema import migrate_proposed_corrections_kind_check

    ev = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": "x"}
    )
    ent = _entity(db, "Legacy", "person", ev)

    # Rebuild the table with the OLD frozen CHECK (no 'edge_review'), simulating
    # a vault created before this ADR, and seed one decided row.
    with db:
        db.execute("DROP TABLE proposed_corrections")
        db.execute(
            """
            CREATE TABLE proposed_corrections (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK (kind IN ('retype', 'merge', 'merge_review')),
                entity_id TEXT NOT NULL REFERENCES entities(id),
                detail TEXT NOT NULL, evidence TEXT NOT NULL, confidence REAL NOT NULL,
                tier TEXT NOT NULL CHECK (tier IN ('auto', 'review')),
                detected_by TEXT NOT NULL, detected_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'proposed'
                    CHECK (status IN ('proposed', 'confirmed', 'rejected', 'applied')),
                decided_at TEXT, decided_by TEXT,
                UNIQUE(kind, entity_id)
            ) STRICT
            """
        )
        db.execute(
            "INSERT INTO proposed_corrections "
            "(id, kind, entity_id, detail, evidence, confidence, tier, detected_by, detected_at) "
            "VALUES ('c1', 'retype', ?, ?, 'weak', 0.5, 'review', 't', '2026-01-01T00:00:00Z')",
            (ent.id, _json.dumps({"to_kind": "organization"})),
        )

    # Pre-migration: an edge_review insert is refused by the frozen CHECK.
    with pytest.raises(Exception, match="CHECK"), db:
        db.execute(
            "INSERT INTO proposed_corrections "
            "(id, kind, entity_id, detail, evidence, confidence, tier, detected_by, detected_at) "
            "VALUES ('e1', 'edge_review', ?, '{}', 'x', 0.3, 'review', 't', '2026-01-02T00:00:00Z')",
            (ent.id,),
        )

    assert migrate_proposed_corrections_kind_check(db) is True
    # The pre-existing row survived the rebuild.
    kept = db.execute("SELECT kind FROM proposed_corrections WHERE id = 'c1'").fetchone()
    assert kept["kind"] == "retype"
    # edge_review inserts now succeed.
    with db:
        db.execute(
            "INSERT INTO proposed_corrections "
            "(id, kind, entity_id, detail, evidence, confidence, tier, detected_by, detected_at) "
            "VALUES ('e1', 'edge_review', ?, '{}', 'x', 0.3, 'review', 't', '2026-01-02T00:00:00Z')",
            (ent.id,),
        )
    # Idempotent: a second migrate is a no-op.
    assert migrate_proposed_corrections_kind_check(db) is False


# ── ADR-0004 S8: tuner-resolved weights ─────────────────────────────────────


def test_promoted_base_rate_changes_next_score(db: sqlite3.Connection, settings: Settings) -> None:
    from afair.substrate import tuner_state

    _ev, _s, _o, edge = _seed_edge(db)
    EdgeConfidenceScorer().run(db, settings)
    before = latest_edge_scores_batch(db, [edge.id])[edge.id].confidence

    # Promote base_rate up → a higher prior → a higher computed score. The
    # scorer builds a fresh registry each cycle, so the next run picks it up.
    tuner_state.write(
        db,
        kind="promote",
        worker="edge_confidence",
        tunable="base_rate",
        old_value=0.70,
        new_value=0.85,
        rationale="test",
    )
    EdgeConfidenceScorer().run(db, settings)
    after = latest_edge_scores_batch(db, [edge.id])[edge.id].confidence
    assert after > before


# ── P0-5a: batched corroboration + SQL-side proposal selection ──────────────


def test_corroboration_batch_matches_single(db: sqlite3.Connection) -> None:
    """count_corroborating_sources_batch must return the same count per edge as
    the single-edge helper it replaces on the scorer's hot path."""
    from afair.substrate import count_corroborating_sources
    from afair.substrate.entities import count_corroborating_sources_batch

    # Two events asserting the SAME triple (shared endpoints) + one unrelated.
    _ev1, subj, obj, edge1 = _seed_edge(db, text="Sajinth runs Athara")
    _ev2, _s, _o, edge2 = _seed_edge(db, text="Sajinth runs Athara again", subj=subj, obj=obj)
    _ev3, _s3, _o3, edge3 = _seed_edge(
        db, text="Priya leads Beta", subject_name="Priya", object_name="Beta", predicate="leads"
    )

    edges = [edge1, edge2, edge3]
    batch = count_corroborating_sources_batch(db, edges)
    for edge in edges:
        single = count_corroborating_sources(
            db,
            subject_id=edge.subject_id,
            predicate=edge.predicate,
            object_id=edge.object_id,
            exclude_event_id=edge.source_event_id,
        )
        assert batch[edge.id] == single
    # Sanity: the two shared-triple edges corroborate each other; the lone one
    # has no corroboration.
    assert batch[edge1.id] == 1
    assert batch[edge2.id] == 1
    assert batch[edge3.id] == 0


def test_corroboration_batch_empty() -> None:
    from afair.substrate.entities import count_corroborating_sources_batch

    # No DB access needed for the empty case.
    assert count_corroborating_sources_batch(None, []) == {}  # type: ignore[arg-type]


def test_propose_edge_reviews_bounded_pool(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proposal selection caps its candidate pool in SQL. Even with far
    more weak edges than the pool, the cycle still proposes exactly K and never
    materializes an unbounded IN-list."""
    monkeypatch.setattr(es, "EDGE_REVIEW_CANDIDATE_POOL", 4)
    for i in range(12):
        _seed_weak_edge(db, i)

    stats = EdgeConfidenceScorer().run(db, settings)
    # Capped at MAX_EDGE_REVIEW_PROPOSALS_PER_CYCLE (3), drawn from the pool.
    assert stats["edge_reviews_proposed"] == es.MAX_EDGE_REVIEW_PROPOSALS_PER_CYCLE


def test_predicate_lower_index_used(db: sqlite3.Connection) -> None:
    """The corroboration fetch must use the LOWER(predicate) expression index,
    not full-scan entity_edges."""
    _seed_edge(db, text="Sajinth runs Athara")
    plan = db.execute(
        """
        EXPLAIN QUERY PLAN
        SELECT e.subject_id, e.object_id, e.source_event_id
        FROM entity_edges e
        LEFT JOIN edge_invalidations i ON i.edge_id = e.id
        WHERE LOWER(e.predicate) = LOWER(?)
          AND i.id IS NULL
        """,
        ("runs",),
    ).fetchall()
    detail = " ".join(str(row["detail"]) for row in plan)
    assert "entity_edges_predicate_lower_idx" in detail
    assert "SCAN e" not in detail


def test_paged_pool_does_not_starve_a_distinct_subject(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REGRESSION (Fable review): with the pool monopolized by many
    low-confidence edges on ONE subject, a distinct subject that sorts past the
    first pool page must still be proposed. The old single-pool code drew only
    EDGE_REVIEW_CANDIDATE_POOL rows once; if those were all the same subject,
    INSERT OR IGNORE absorbed all but one and rank-(pool+1) subjects were never
    reached — this cycle or any future cycle. Keyset paging restores
    walk-until-K."""
    from afair.substrate import read_pending_corrections

    monkeypatch.setattr(es, "EDGE_REVIEW_CANDIDATE_POOL", 4)

    # Four weak edges all on the SAME subject (Person0), distinct objects, so
    # they fill the first page but collapse to one proposal via UNIQUE(subject).
    _ev, subj0, _obj0, _e0 = _seed_edge(
        db,
        text="Person0 is loosely connected to the Thing0",
        predicate="is loosely connected to the",
        extraction_confidence=None,
        with_interpretation=False,
        subj_conf=0.5,
        obj_conf=0.5,
        subject_name="Person0",
        object_name="Thing0",
    )
    record_edge_serves(db, [_e0.id])
    for j in range(1, 4):
        _e, _s, _o, edge_j = _seed_edge(
            db,
            text=f"Person0 is loosely connected to the Thing{j}",
            predicate="is loosely connected to the",
            extraction_confidence=None,
            with_interpretation=False,
            subj=subj0,
            subj_conf=0.5,
            obj_conf=0.5,
            object_name=f"Thing{j}",
        )
        record_edge_serves(db, [edge_j.id])
    # One weak edge on a DISTINCT subject, created last → sorts past page 1.
    _seed_weak_edge(db, 99)

    EdgeConfidenceScorer().run(db, settings)

    reviews = [p for p in read_pending_corrections(db) if p.kind == "edge_review"]
    subjects = {p.detail["subject_name"] for p in reviews}
    assert "Person99" in subjects  # past-pool subject is NOT starved
    # The monopolizing subject gets exactly one open proposal, not four.
    assert sum(1 for p in reviews if p.detail["subject_name"] == "Person0") == 1
