"""Belief revision for the derived layer (ADR-0002).

Two layers: the pure entrenchment / auto-confirm / trust-state logic, and the
append-only edge_reviews write path (confirm records a verdict; reject also
retracts the edge through the existing invalidation path).
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from afair.substrate import (
    latest_edge_review,
    open_db,
    read_edge_invalidations,
    record_edge_review,
    write_entity,
    write_entity_edge,
    write_event,
)
from afair.substrate.belief import (
    Entrenchment,
    TrustState,
    auto_confirm,
    predicate_is_crisp,
    resolve_trust,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


# ── pure logic ──────────────────────────────────────────────────────────────


def test_entrenchment_is_ordered_foreign_lowest() -> None:
    assert Entrenchment.FOREIGN_IMPORT < Entrenchment.AGENT_DERIVED
    assert Entrenchment.AGENT_DERIVED < Entrenchment.USER_STATED
    assert Entrenchment.USER_STATED < Entrenchment.USER_CONFIRMED


def test_predicate_crispness_catches_verbose_profile_language() -> None:
    # Real relations: short verb phrases — crisp.
    assert predicate_is_crisp("runs")
    assert predicate_is_crisp("is design partner for")
    # The verbose profile-language tell (5+ words) is caught.
    assert not predicate_is_crisp("is tech person in circle of")
    assert not predicate_is_crisp("is Tech-Person in same circle as")
    assert not predicate_is_crisp("")
    assert not predicate_is_crisp("   ")  # whitespace-only is empty after split
    # Honest limitation: crispness is only a SECONDARY signal. Short
    # confabulations ("active in", "shares role with") pass it — those are
    # caught by the evidence gate (Track 1, the primary defense), not by
    # word count.
    assert predicate_is_crisp("shares role with")  # 3 words: passes here


def test_auto_confirm_trusts_only_grounded_crisp_nonforeign() -> None:
    base = {
        "confidence": 0.85,
        "predicate": "runs",
        "source_entrenchment": Entrenchment.AGENT_DERIVED,
        "has_evidence": True,
    }
    assert auto_confirm(**base)  # the happy path is trusted

    # Any single defect drops it to the review queue.
    assert not auto_confirm(**{**base, "source_entrenchment": Entrenchment.FOREIGN_IMPORT})
    assert not auto_confirm(**{**base, "has_evidence": False})
    assert not auto_confirm(**{**base, "confidence": 0.5})
    assert not auto_confirm(**{**base, "predicate": "is tech person in circle of"})
    assert not auto_confirm(**{**base, "predicate": ""})  # malformed empty predicate


def test_resolve_trust_precedence() -> None:
    # The invalidation is the canonical reject signal, and it is a defeater: it
    # wins even over a prior confirm (a source correction retracts the edge).
    assert (
        resolve_trust(latest_verdict=None, is_invalidated=True, auto_confirmed=True)
        == TrustState.REJECTED
    )
    assert (
        resolve_trust(latest_verdict="confirm", is_invalidated=True, auto_confirmed=True)
        == TrustState.REJECTED
    )
    # Explicit confirm beats the auto policy (when not invalidated).
    assert (
        resolve_trust(latest_verdict="confirm", is_invalidated=False, auto_confirmed=False)
        == TrustState.CONFIRMED
    )
    # No review → the auto policy decides.
    assert (
        resolve_trust(latest_verdict=None, is_invalidated=False, auto_confirmed=True)
        == TrustState.AUTO_CONFIRMED
    )
    assert (
        resolve_trust(latest_verdict=None, is_invalidated=False, auto_confirmed=False)
        == TrustState.PROPOSED
    )


# ── the edge_reviews write path ───────────────────────────────────────────────


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path)
    try:
        yield conn
    finally:
        conn.close()


def _edge(db: sqlite3.Connection) -> str:
    ev = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": "x"}
    )
    s = write_entity(
        db,
        canonical_name="Sajinth",
        kind="person",
        created_by="t",
        source_event_id=ev.id,
        confidence=0.9,
    )
    o = write_entity(
        db,
        canonical_name="Athara",
        kind="organization",
        created_by="t",
        source_event_id=ev.id,
        confidence=0.9,
    )
    edge = write_entity_edge(
        db,
        subject_id=s.id,
        predicate="runs",
        object_id=o.id,
        source_event_id=ev.id,
        discovered_by="entity_canonicalizer:v0",
        confidence=0.8,
    )
    assert edge is not None
    return edge.id


def test_confirm_records_verdict_no_invalidation(db: sqlite3.Connection) -> None:
    edge_id = _edge(db)
    record_edge_review(db, edge_id=edge_id, verdict="confirm", reviewed_by="operator")
    assert latest_edge_review(db, edge_id) == "confirm"
    assert read_edge_invalidations(db, edge_id) == []  # confirm never retracts


def test_reject_records_verdict_and_invalidates(db: sqlite3.Connection) -> None:
    edge_id = _edge(db)
    record_edge_review(
        db, edge_id=edge_id, verdict="reject", reviewed_by="operator", reason="wrong"
    )
    assert latest_edge_review(db, edge_id) == "reject"
    # Reject flows through the existing defeasible-retraction path.
    invs = read_edge_invalidations(db, edge_id)
    assert len(invs) == 1
    assert invs[0].reason == "wrong"


def test_latest_review_wins(db: sqlite3.Connection) -> None:
    edge_id = _edge(db)
    record_edge_review(db, edge_id=edge_id, verdict="reject", reviewed_by="operator")
    record_edge_review(db, edge_id=edge_id, verdict="confirm", reviewed_by="operator")
    assert latest_edge_review(db, edge_id) == "confirm"  # newest verdict is current


def test_reviews_are_append_only(db: sqlite3.Connection) -> None:
    edge_id = _edge(db)
    review = record_edge_review(db, edge_id=edge_id, verdict="confirm", reviewed_by="operator")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE edge_reviews SET verdict = 'reject' WHERE id = ?", (review.id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM edge_reviews WHERE id = ?", (review.id,))


def test_invalid_verdict_rejected(db: sqlite3.Connection) -> None:
    edge_id = _edge(db)
    with pytest.raises(ValueError, match="verdict must be"):
        record_edge_review(db, edge_id=edge_id, verdict="maybe", reviewed_by="operator")


def test_review_against_missing_edge_is_clean_error(db: sqlite3.Connection) -> None:
    """A stale/typo'd edge_id fails with a domain error, not a raw FK
    IntegrityError from deep in the insert."""
    with pytest.raises(ValueError, match="entity_edge not found"):
        record_edge_review(db, edge_id="entity_edge:nope", verdict="confirm", reviewed_by="op")


def test_reject_is_atomic_no_half_commit(db: sqlite3.Connection) -> None:
    """The verdict and its invalidation are one transaction: if the
    invalidation fails, the verdict must NOT persist. A reject that left a
    verdict without an invalidation would keep the edge live in the graph reads
    while latest_edge_review said 'reject' — the exact desync ADR-0002 prevents.

    Forced cleanly: a non-existent source_event_id violates the
    edge_invalidations FK and raises mid-transaction.
    """
    edge_id = _edge(db)
    with pytest.raises(sqlite3.IntegrityError):
        record_edge_review(
            db,
            edge_id=edge_id,
            verdict="reject",
            reviewed_by="operator",
            source_event_id="evt:does-not-exist",
        )
    # The whole transaction rolled back — neither row landed.
    assert latest_edge_review(db, edge_id) is None
    assert read_edge_invalidations(db, edge_id) == []
