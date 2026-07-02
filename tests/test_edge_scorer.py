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
    subj = subj or _entity(conn, "Sajinth", "person", ev)
    obj = obj or _entity(conn, "Athara", "organization", ev)
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
