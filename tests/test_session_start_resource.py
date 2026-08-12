"""Tests for the afair://session-start MCP resource.

Verifies the payload shape, the salience-ranked event surfacing, the
open_threads pull from the consolidator, and the cache semantics.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from afair.agents.mode_switcher import MODE_CEN, MODE_DMN, MODE_SWITCHER_ORIGIN
from afair.agents.salience import SalienceWorker
from afair.mcp import resources
from afair.settings import Settings
from afair.substrate import open_db, write_event, write_event_temporal

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


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
    )


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    resources.clear_cache()


# ── payload shape ─────────────────────────────────────────────────────────


def test_empty_vault_yields_default_payload(db: sqlite3.Connection) -> None:
    """Fresh vault: no salient events, no threads, mode = DMN."""
    payload = resources.read_session_start(db)
    assert payload["mode"] == MODE_DMN
    assert payload["recent_salient_events"] == []
    assert payload["open_threads"] == []
    assert payload["vault_size"] == {"total": 0, "remembers": 0, "observes": 0}
    assert payload["cumulative_salience"] == 0.0
    assert payload["pending_corrections"] == []
    assert "instructions" in payload


def test_pending_corrections_surface_with_prompt(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """An open audit proposal shows up at session start with a yes/no prompt
    and decide-instructions, so the AI can raise it proactively."""
    from afair.agents.entity_audit import EntityAuditWorker
    from afair.substrate import write_entity

    ev = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": "maxime.team"}
    )
    write_entity(
        db,
        canonical_name="maxime.team",
        kind="person",
        created_by="t",
        source_event_id=ev.id,
        confidence=0.8,
    )
    EntityAuditWorker().run(db, settings)

    payload = resources.build_session_start_payload(db)
    pending = payload["pending_corrections"]
    assert len(pending) == 1
    assert pending[0]["kind"] == "retype"
    assert "re-type" in pending[0]["prompt"]
    # Fix 3: a single fresh high-value item is below the nudge threshold, so the
    # decide-nudge sentence is suppressed — the item still rides the structured
    # payload, and the AI is told to surface it only if it fits (not as a count).
    assert "afair.recall(decide=" not in payload["instructions"]
    assert "only if it fits" in payload["instructions"]


def test_top_salient_query_uses_producer_index(db: sqlite3.Connection) -> None:
    """P2a: _read_top_salient runs on every connect; its
    `WHERE produced_by = ? ORDER BY produced_at DESC` must ride the new
    interpretations_producer_produced_idx and skip the temp-sort."""
    from afair.agents.salience import SALIENCE_PRODUCED_BY

    plan = db.execute(
        """
        EXPLAIN QUERY PLAN
        SELECT e.id, e.content_hash, e.created_at, e.kind, e.payload,
               i.extraction AS salience_extraction
        FROM interpretations i
        JOIN events e ON e.content_hash = i.event_hash
        WHERE i.produced_by = ?
        ORDER BY i.produced_at DESC
        LIMIT ?
        """,
        (SALIENCE_PRODUCED_BY, 30),
    ).fetchall()
    detail = " ".join(str(row["detail"]) for row in plan)
    assert "interpretations_producer_produced_idx" in detail
    assert "USE TEMP B-TREE" not in detail


def test_salient_events_surface_top_n_by_score(db: sqlite3.Connection, settings: Settings) -> None:
    """Top-salience events appear, ordered most-salient first."""
    # Mix: 3 high-signal (decision + compound), 2 plain texts.
    for i in range(3):
        write_event(
            db,
            origin="user",
            kind="remember",
            payload={
                "content_type": "compound",
                "parts": [{"type": "text", "text": f"decision part {i}"}],
                "type_hint": "decision",
            },
        )
    for i in range(2):
        write_event(
            db,
            origin="user",
            kind="remember",
            payload={"content_type": "text", "text": f"casual note {i}"},
        )
    SalienceWorker().run(db, settings)

    payload = resources.read_session_start(db)
    events = payload["recent_salient_events"]
    assert len(events) == 5
    # Score-ordered descending.
    scores = [e["salience"] for e in events]
    assert scores == sorted(scores, reverse=True)
    # High-signal events score higher than plain.
    high_signal = [e for e in events if e.get("type_hint") == "decision"]
    casual = [e for e in events if e.get("type_hint") != "decision"]
    assert all(h["salience"] > c["salience"] for h in high_signal for c in casual)


def test_recent_salient_capped_at_limit(db: sqlite3.Connection, settings: Settings) -> None:
    """Even with 20 scored events, the resource caps at _SALIENT_LIMIT (10)."""
    for i in range(20):
        write_event(
            db,
            origin="user",
            kind="remember",
            payload={"content_type": "text", "text": f"event {i}"},
        )
    SalienceWorker().run(db, settings)

    payload = resources.read_session_start(db)
    assert len(payload["recent_salient_events"]) <= 10


def test_event_preview_truncates_long_text(db: sqlite3.Connection, settings: Settings) -> None:
    long_text = "x" * 500
    write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": long_text},
    )
    SalienceWorker().run(db, settings)
    payload = resources.read_session_start(db)
    assert len(payload["recent_salient_events"][0]["preview"]) <= 200


def test_compound_event_preview_uses_first_text_part(
    db: sqlite3.Connection, settings: Settings
) -> None:
    write_event(
        db,
        origin="user",
        kind="remember",
        payload={
            "content_type": "compound",
            "parts": [
                {"type": "text", "text": "transcript content here"},
                {"type": "blob-ref", "blob_hash": "sha256:" + "0" * 64, "mime": "image/png"},
            ],
        },
    )
    SalienceWorker().run(db, settings)
    payload = resources.read_session_start(db)
    preview = payload["recent_salient_events"][0]["preview"]
    assert "transcript content" in preview


def test_observe_event_preview_renders_action_subject(
    db: sqlite3.Connection, settings: Settings
) -> None:
    write_event(
        db,
        origin="agent",
        kind="observe",
        payload={
            "content_type": "event",
            "action": "edit_file",
            "subject": "events.py",
            "result": "added inline-vs-spill logic",
        },
    )
    SalienceWorker().run(db, settings)
    payload = resources.read_session_start(db)
    preview = payload["recent_salient_events"][0]["preview"]
    assert "edit_file" in preview and "events.py" in preview


# ── mode + cumulative salience ────────────────────────────────────────────


def test_mode_reflects_substrate_state(db: sqlite3.Connection) -> None:
    # Seed a CEN-transition event by hand.
    write_event(
        db,
        origin=MODE_SWITCHER_ORIGIN,
        kind="observe",
        payload={
            "content_type": "event",
            "action": "mode_switched",
            "subject": MODE_CEN,
            "result": "test seed",
        },
    )
    payload = resources.read_session_start(db)
    assert payload["mode"] == MODE_CEN


def test_cumulative_salience_sums_surfaced_events(
    db: sqlite3.Connection, settings: Settings
) -> None:
    for i in range(3):
        write_event(
            db,
            origin="user",
            kind="remember",
            payload={"content_type": "text", "text": f"event {i}"},
        )
    SalienceWorker().run(db, settings)
    payload = resources.read_session_start(db)
    expected = sum(e["salience"] for e in payload["recent_salient_events"])
    assert payload["cumulative_salience"] == pytest.approx(expected, rel=0.01)


# ── open_threads from consolidator ────────────────────────────────────────


def _seed_consolidation(db: sqlite3.Connection, threads: list[str]) -> None:
    """Write a real consolidation via the production path.

    The Consolidator emits its digest as a substrate EVENT
    (kind='consolidation') whose payload carries open_threads — NOT an
    interpretation row. Seeding through _write_consolidation ensures the
    test exercises the layer the reader actually reads (P0-4)."""
    from afair.agents.consolidator import _DaySummary, _write_consolidation

    source = write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "a day's worth of activity"},
    )
    _write_consolidation(
        db,
        target_day=datetime.now(UTC).date(),
        events=[source],
        summary=_DaySummary(
            narrative="Summary of the day.",
            themes=["test theme"],
            open_threads=threads,
        ),
    )


def test_open_threads_surfaces_consolidator_output(db: sqlite3.Connection) -> None:
    _seed_consolidation(db, ["finish Stripe webhook", "rotate prod tokens"])
    payload = resources.read_session_start(db)
    assert "finish Stripe webhook" in payload["open_threads"]
    assert "rotate prod tokens" in payload["open_threads"]


def test_open_threads_handles_dict_format(db: sqlite3.Connection) -> None:
    """Some consolidator versions emit {text: ...} shaped threads. The reader's
    tolerance loop must flatten them. Seeded as a raw consolidation event
    because _DaySummary only carries list[str]."""
    write_event(
        db,
        origin="agent",
        kind="consolidation",
        payload={
            "content_type": "text",
            "text": "synthetic consolidation",
            "open_threads": [
                {"text": "merge the docs branch"},
                {"text": "ping support@"},
            ],
        },
    )
    payload = resources.read_session_start(db)
    assert "merge the docs branch" in payload["open_threads"]
    assert "ping support@" in payload["open_threads"]


def test_open_threads_uses_latest_consolidation(db: sqlite3.Connection) -> None:
    """Two consolidations on different days → the most recent one's threads
    are surfaced (ORDER BY created_at DESC LIMIT 1)."""
    from afair.agents.consolidator import _DaySummary, _write_consolidation

    older = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": "day one"}
    )
    _write_consolidation(
        db,
        target_day=datetime.now(UTC).date() - timedelta(days=1),
        events=[older],
        summary=_DaySummary(narrative="older", themes=["a"], open_threads=["stale thread"]),
    )
    newer = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": "day two"}
    )
    _write_consolidation(
        db,
        target_day=datetime.now(UTC).date(),
        events=[newer],
        summary=_DaySummary(narrative="newer", themes=["b"], open_threads=["fresh thread"]),
    )

    payload = resources.read_session_start(db)
    assert "fresh thread" in payload["open_threads"]
    assert "stale thread" not in payload["open_threads"]


def test_open_threads_capped_at_limit(db: sqlite3.Connection) -> None:
    threads = [f"thread {i}" for i in range(20)]
    _seed_consolidation(db, threads)
    payload = resources.read_session_start(db)
    assert len(payload["open_threads"]) <= 8


# ── cache ─────────────────────────────────────────────────────────────────


def test_cache_returns_same_payload_on_repeated_read(
    db: sqlite3.Connection, settings: Settings
) -> None:
    write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "event"},
    )
    SalienceWorker().run(db, settings)
    a = resources.read_session_start(db)
    b = resources.read_session_start(db)
    assert a == b


def test_cache_invalidates_on_new_event(db: sqlite3.Connection, settings: Settings) -> None:
    """A new remember after a session-start read returns fresh data."""
    write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "event 1"},
    )
    SalienceWorker().run(db, settings)
    payload_before = resources.read_session_start(db)
    assert payload_before["vault_size"]["total"] == 1

    write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "event 2"},
    )
    # Cache key uses latest event id — the new event invalidates implicitly.
    payload_after = resources.read_session_start(db)
    assert payload_after["vault_size"]["total"] == 2


def test_session_start_includes_upcoming(db: sqlite3.Connection) -> None:
    """A recurring memory coming due soon surfaces in the upcoming field."""
    event = write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "Mara's birthday"},
    )
    soon = (datetime.now(UTC) + timedelta(days=4)).isoformat()
    write_event_temporal(
        db,
        event_id=event.id,
        event_hash=event.content_hash,
        temporal_class="recurring",
        confidence=0.9,
        computed_by="temporal:v1",
        event_time=soon,
        recurrence_rule="FREQ=YEARLY",
    )
    payload = resources.build_session_start_payload(db)
    assert "upcoming" in payload
    ids = {item["event_id"] for item in payload["upcoming"]}
    assert event.id in ids
    item = next(i for i in payload["upcoming"] if i["event_id"] == event.id)
    assert item["temporal_class"] == "recurring"
    assert item["when"] is not None
    assert "upcoming" in payload["instructions"]


def test_session_start_upcoming_empty_when_nothing_due(db: sqlite3.Connection) -> None:
    write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "a plain note"},
    )
    payload = resources.build_session_start_payload(db)
    assert payload["upcoming"] == []


# ── Fix 3: value-ranked, rate-limited pending nudge ──────────────────────────


def _seed_retype(db: sqlite3.Connection, settings: Settings, name: str = "maxime.team") -> None:
    """Seed one high-value retype proposal via the entity-audit worker."""
    from afair.agents.entity_audit import EntityAuditWorker
    from afair.substrate import write_entity

    ev = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": name}
    )
    write_entity(
        db,
        canonical_name=name,
        kind="person",
        created_by="t",
        source_event_id=ev.id,
        confidence=0.8,
    )
    EntityAuditWorker().run(db, settings)


def _seed_edge_review(db: sqlite3.Connection, settings: Settings, i: int = 1) -> None:
    """Seed one low-value edge_review proposal via the edge scorer."""
    from afair.substrate import (
        record_edge_serves,
        write_entity,
        write_entity_edge,
        write_entity_mention,
    )

    ev = write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": f"P{i} is loosely connected to the O{i}"},
    )
    subj = write_entity(
        db,
        canonical_name=f"P{i}",
        kind="person",
        created_by="t",
        source_event_id=ev.id,
        confidence=0.5,
    )
    obj = write_entity(
        db,
        canonical_name=f"O{i}",
        kind="organization",
        created_by="t",
        source_event_id=ev.id,
        confidence=0.5,
    )
    for endpoint in (subj, obj):
        write_entity_mention(
            db,
            entity_id=endpoint.id,
            event_id=ev.id,
            event_hash=ev.content_hash,
            surface_form=endpoint.canonical_name,
            canonicalized_by="t",
            match_method="exact",
            confidence=0.5,
        )
    edge = write_entity_edge(
        db,
        subject_id=subj.id,
        predicate="is loosely connected to the",
        object_id=obj.id,
        source_event_id=ev.id,
        discovered_by="t",
        confidence=0.3,
    )
    assert edge is not None
    record_edge_serves(db, [edge.id])
    from afair.agents.edge_scorer import EdgeConfidenceScorer

    EdgeConfidenceScorer().run(db, settings)


def _seed_conflict(db: sqlite3.Connection) -> None:
    """Enqueue one open conflict-resolution proposal."""
    from afair.substrate import enqueue_conflict_proposal

    a = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": "I live in A"}
    )
    b = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": "I live in B"}
    )
    enqueue_conflict_proposal(
        db,
        event_a_id=a.id,
        event_a_hash=a.content_hash,
        event_b_id=b.id,
        event_b_hash=b.content_hash,
        newer_hash=b.content_hash,
        flag_verdict="supersedes",
        reason="location changed",
        confidence=0.9,
        detected_by="conflict_resolver:v0",
    )


def test_edge_reviews_ride_payload_but_not_the_nudge(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """Only edge_reviews pending: they appear in pending_corrections, but NO
    review-ask sentence fires — they expire on their own."""
    _seed_edge_review(db, settings)
    payload = resources.build_session_start_payload(db)
    kinds = {p["kind"] for p in payload["pending_corrections"]}
    assert "edge_review" in kinds
    # No decide-nudge for the edge_review; the static auto-expire line is present.
    assert "afair.recall(decide=" not in payload["instructions"]
    assert "expire on their own" in payload["instructions"]


def test_conflict_is_itemized_first_and_always_nudges(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """A pending conflict is listed FIRST and bypasses the rate limit — a memory
    conflict always earns a mention."""
    _seed_retype(db, settings)  # a high-value item, below the growth threshold alone
    _seed_conflict(db)
    payload = resources.build_session_start_payload(db)
    pending = payload["pending_corrections"]
    assert pending[0]["kind"] == "conflict"  # conflicts first
    assert "a memory conflict needs your call" in payload["instructions"].lower()
    assert "afair.recall(decide=" in payload["instructions"]


def test_nudge_is_rate_limited_and_self_acknowledges(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """Three fresh high-value items cross NUDGE_MIN_NEW so the nudge fires and
    records the marker; an immediate rebuild with the SAME queue does not
    re-fire (cooldown + no growth)."""
    for n in ("alpha.team", "beta.team", "gamma.team"):
        _seed_retype(db, settings, name=n)
    first = resources.build_session_start_payload(db)
    assert "afair.recall(decide=" in first["instructions"]  # fired (3 >= NUDGE_MIN_NEW)

    # Immediate rebuild, unchanged queue → cooldown + no growth → suppressed.
    resources.clear_cache()
    second = resources.build_session_start_payload(db)
    assert "afair.recall(decide=" not in second["instructions"]
    assert "only if it fits" in second["instructions"]


def test_nudge_returns_after_growth_and_cooldown(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the nudge is shown, it returns once the queue grows by NUDGE_MIN_NEW
    AND the cooldown has elapsed (simulated by back-dating the marker)."""
    from afair.substrate import watermarks

    for n in ("alpha.team", "beta.team", "gamma.team"):
        _seed_retype(db, settings, name=n)
    resources.build_session_start_payload(db)  # fires, records marker at total=3

    # Back-date the marker 8 days so the cooldown has elapsed.
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    wm = watermarks.read_watermark(db, resources._PENDING_NUDGE_MARKER)
    assert wm is not None
    with db:
        db.execute(
            "UPDATE worker_watermarks SET through_created_at = ? WHERE worker = ?",
            (old, resources._PENDING_NUDGE_MARKER),
        )
    # Grow the high-value queue by 3 more.
    for n in ("delta.team", "epsilon.team", "zeta.team"):
        _seed_retype(db, settings, name=n)
    resources.clear_cache()
    payload = resources.build_session_start_payload(db)
    assert "afair.recall(decide=" in payload["instructions"]  # returned


def test_resolving_a_conflict_does_not_inflate_the_nudge_baseline(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The nudge growth baseline tracks ONLY the non-conflict high-value total.

    A conflict bypasses the growth gate unconditionally, so it must not be folded
    into the stored baseline: otherwise resolving the conflict deflates the count
    and a later legitimate +NUDGE_MIN_NEW non-conflict growth is wrongly
    suppressed. Here the nudge first fires via the conflict bypass (recording the
    marker), the conflict is resolved, and a later +3 non-conflict growth must
    still nudge."""
    from afair.substrate import (
        decide_conflict_proposal,
        read_pending_conflict_proposals,
        watermarks,
    )

    # A conflict + 3 non-conflict high-value items. The nudge fires via the
    # conflict bypass and records the marker.
    for n in ("alpha.team", "beta.team", "gamma.team"):
        _seed_retype(db, settings, name=n)
    _seed_conflict(db)
    first = resources.build_session_start_payload(db)
    assert "a memory conflict needs your call" in first["instructions"].lower()

    # The marker stored the NON-CONFLICT baseline (3), not the combined total (4).
    # read_watermark returns (through_created_at, through_id); the stored total
    # rides in through_id.
    wm = watermarks.read_watermark(db, resources._PENDING_NUDGE_MARKER)
    assert wm is not None
    assert wm[1] == "3"

    # Resolve the conflict — the conflict count drops to 0, the baseline is
    # unchanged (still 3, conflict-free).
    proposals = read_pending_conflict_proposals(db, limit=10)
    assert len(proposals) == 1
    decide_conflict_proposal(db, proposal_id=proposals[0].id, verdict="confirm")

    # Back-date the marker past the cooldown, then grow the non-conflict queue by
    # NUDGE_MIN_NEW (3 more retypes → non-conflict total 6). Growth is measured
    # against the conflict-free baseline of 3, so 6 - 3 = 3 >= NUDGE_MIN_NEW fires.
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    with db:
        db.execute(
            "UPDATE worker_watermarks SET through_created_at = ? WHERE worker = ?",
            (old, resources._PENDING_NUDGE_MARKER),
        )
    for n in ("delta.team", "epsilon.team", "zeta.team"):
        _seed_retype(db, settings, name=n)
    resources.clear_cache()
    payload = resources.build_session_start_payload(db)
    assert "afair.recall(decide=" in payload["instructions"]  # legitimate nudge fired


def test_pending_corrections_count_stays_true_total(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """The value split never changes the grand total the recall handler serves:
    conflicts + high-value + edge_reviews all count into the queue."""
    from afair.substrate import (
        count_pending_conflict_proposals,
        count_pending_corrections,
    )

    _seed_retype(db, settings)
    _seed_edge_review(db, settings)
    _seed_conflict(db)
    total = count_pending_corrections(db) + count_pending_conflict_proposals(db)
    # retype (1) + edge_review (1) + conflict (1) = 3.
    assert total == 3
