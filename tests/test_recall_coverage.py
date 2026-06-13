"""Recall coverage / honesty layer — "what your memory doesn't know yet".

Unit tests over ``_compute_coverage`` (pure: events + invalidation/conflict
maps → RecallCoverage). The deterministic first pass of BUILD #1.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from afair.mcp.handlers import STALENESS_CAVEAT_DAYS, _compute_coverage
from afair.substrate.events import Event


def _ev(n: int, *, age_days: int = 0) -> Event:
    created = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
    return Event(
        id=f"e{n}",
        content_hash=f"sha256:{n:064d}",
        created_at=created,
        origin="agent",
        kind="remember",
        payload={"content_type": "text", "text": f"event {n}"},
        schema_version=1,
    )


def test_no_hits_flags_thin_and_says_so() -> None:
    cov = _compute_coverage([], {}, {})
    assert cov.thin_evidence is True
    assert cov.caveats and "doesn't hold this yet" in cov.caveats[0]


def test_single_hit_is_thin_evidence() -> None:
    cov = _compute_coverage([_ev(1)], {}, {})
    assert cov.thin_evidence is True
    assert any("Thin evidence" in c for c in cov.caveats)


def test_fresh_plentiful_no_conflicts_has_no_caveats() -> None:
    events = [_ev(1), _ev(2), _ev(3)]
    cov = _compute_coverage(events, {}, {})
    assert cov.caveats == []
    assert cov.thin_evidence is False
    assert cov.unresolved_contradictions == 0
    assert cov.stale_newest_event_days == 0


def test_stale_when_even_newest_is_old() -> None:
    old = STALENESS_CAVEAT_DAYS + 10
    events = [_ev(1, age_days=old), _ev(2, age_days=old + 30)]
    cov = _compute_coverage(events, {}, {})
    assert cov.stale_newest_event_days == old
    assert any("out of date" in c for c in cov.caveats)


def test_unresolved_contradiction_is_counted_temporal_update_is_not() -> None:
    events = [_ev(1), _ev(2), _ev(3)]
    conflicts = {
        events[0].content_hash: [{"verdict": "contradiction", "reason": "x", "confidence": 0.9}],
        # a temporal update must NOT count as an unresolved conflict
        events[1].content_hash: [
            {"verdict": "temporal_supersession", "reason": "y", "confidence": 0.9}
        ],
    }
    cov = _compute_coverage(events, {}, conflicts)
    assert cov.unresolved_contradictions == 1
    assert any("unresolved tension" in c for c in cov.caveats)
    # the supersession caveat is surfaced even though it's not a "conflict"
    assert any("supersedes an older" in c for c in cov.caveats)


def test_legacy_verdict_still_counts_as_unresolved() -> None:
    events = [_ev(1), _ev(2)]
    conflicts = {events[0].content_hash: [{"verdict": "contradicts", "confidence": 0.9}]}
    cov = _compute_coverage(events, {}, conflicts)
    assert cov.unresolved_contradictions == 1


def test_different_referent_surfaces_caveat_without_being_a_conflict() -> None:
    events = [_ev(1), _ev(2)]
    conflicts = {events[0].content_hash: [{"verdict": "different_referent", "confidence": 0.9}]}
    cov = _compute_coverage(events, {}, conflicts)
    assert cov.unresolved_contradictions == 0
    assert any("different things" in c for c in cov.caveats)


def test_invalidated_hits_are_counted() -> None:
    events = [_ev(1), _ev(2), _ev(3)]
    invalidations = {events[0].content_hash: object()}
    cov = _compute_coverage(events, invalidations, {})
    assert cov.invalidated_hits == 1
    assert any("superseded" in c for c in cov.caveats)
