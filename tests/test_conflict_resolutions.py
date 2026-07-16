"""Phase 2 backend: operator resolution of synthesis conflicts (ADR-0008).

Covers the enqueue + backfill (resolver), the confirm/reject/retract decisions
through the frozen decide_correction verb (cfl_ dispatch), append-only
byte-identical assertions (I2), idempotency + guards, the count-drop on
resolution, pruner eligibility, export exclusion, and MCP pending-view parity.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from afair.agents.conflict_resolver import (
    ConflictPair,
    _backfill_conflict_proposals,
    _write_verdict,
    flag_is_unresolved,
    read_conflicts_batch,
)
from afair.settings import Settings
from afair.substrate import (
    count_pending_conflict_proposals,
    decide_correction,
    enqueue_conflict_proposal,
    open_db,
    read_pending_conflict_proposals,
    write_event,
)
from afair.substrate.conflict_resolutions import pair_key_for

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


def _res_producer(a_hash: str, b_hash: str) -> str:
    """The resolution interpretation's producer for a pair — encodes the pair
    key (min:max), not event B alone (ADR-0008 fix for shared-event collision)."""
    return f"conflict_resolution:v1:{pair_key_for(a_hash, b_hash)}"


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


def _event(conn: sqlite3.Connection, text: str, created_at: str) -> tuple[str, str]:
    ev = write_event(
        conn,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": text},
        created_at=created_at,
    )
    return ev.id, ev.content_hash


def _pair(conn: sqlite3.Connection) -> tuple[str, str, str, str]:
    """An older event A + a newer event B, both real. Returns (a_id,a_hash,b_id,b_hash)."""
    a_id, a_hash = _event(conn, "Sajinth is CEO", "2025-01-01T00:00:00+00:00")
    b_id, b_hash = _event(conn, "Sajinth is CTO", "2026-01-01T00:00:00+00:00")
    return a_id, a_hash, b_id, b_hash


def _enqueue(conn: sqlite3.Connection, a: tuple[str, str, str, str]) -> str:
    a_id, a_hash, b_id, b_hash = a
    pid = enqueue_conflict_proposal(
        conn,
        event_a_id=a_id,
        event_a_hash=a_hash,
        event_b_id=b_id,
        event_b_hash=b_hash,
        newer_hash=b_hash,  # B is newer
        flag_verdict="conflicts",
        reason="role changed at the same time; dates don't explain it",
        confidence=0.9,
        detected_by="conflict_resolver:v0",
    )
    assert pid is not None
    return pid


def _write_flag_verdict(
    conn: sqlite3.Connection, a: tuple[str, str, str, str], verdict: str
) -> None:
    """Write a real conflict_flag interpretation for the pair (for backfill/read tests)."""
    a_id, a_hash, b_id, b_hash = a
    from afair.substrate.events import read_event_by_hash

    event_a = read_event_by_hash(conn, a_hash)
    assert event_a is not None
    _write_verdict(
        conn,
        event_a=event_a,
        pair=ConflictPair(
            event_a_hash=a_hash,
            event_b_hash=b_hash,
            event_a_id=a_id,
            event_b_id=b_id,
            verdict=verdict,
            reason="r",
            confidence=0.9,
        ),
    )


# ── enqueue ──────────────────────────────────────────────────────────────────


def test_enqueue_is_anti_re_nag(db: sqlite3.Connection) -> None:
    a = _pair(db)
    pid = _enqueue(db, a)
    assert count_pending_conflict_proposals(db) == 1
    # Second enqueue for the same pair (either hash order) is a no-op.
    a_id, a_hash, b_id, b_hash = a
    again = enqueue_conflict_proposal(
        db,
        event_a_id=b_id,
        event_a_hash=b_hash,
        event_b_id=a_id,
        event_b_hash=a_hash,
        newer_hash=b_hash,
        flag_verdict="conflicts",
        reason="r",
        confidence=0.9,
        detected_by="conflict_resolver:v0",
    )
    assert again is None
    assert count_pending_conflict_proposals(db) == 1
    assert read_pending_conflict_proposals(db)[0].id == pid


def test_pair_key_is_order_independent(db: sqlite3.Connection) -> None:
    assert pair_key_for("bbb", "aaa") == pair_key_for("aaa", "bbb") == "aaa:bbb"


# ── backfill ─────────────────────────────────────────────────────────────────


def test_backfill_makes_existing_unresolved_flags_decidable(db: sqlite3.Connection) -> None:
    a = _pair(db)
    _write_flag_verdict(db, a, "conflicts")  # an unresolved conflict flag, no proposal yet
    assert count_pending_conflict_proposals(db) == 0
    made = _backfill_conflict_proposals(db, max_pairs=50)
    assert made == 1
    assert count_pending_conflict_proposals(db) == 1


def test_backfill_skips_resolved_verdicts(db: sqlite3.Connection) -> None:
    a = _pair(db)
    _write_flag_verdict(db, a, "confirms")  # not an unresolved conflict
    made = _backfill_conflict_proposals(db, max_pairs=50)
    assert made == 0
    assert count_pending_conflict_proposals(db) == 0


def test_backfill_skips_already_decided_pairs(db: sqlite3.Connection) -> None:
    a = _pair(db)
    _write_flag_verdict(db, a, "conflicts")
    pid = _enqueue(db, a)
    decide_correction(db, proposal_id=pid, verdict="confirm")  # applied → resolution interp written
    made = _backfill_conflict_proposals(db, max_pairs=50)
    assert made == 0  # queue anti-re-nag + resolution guard both hold


# ── decide via decide_correction (cfl_ dispatch) ─────────────────────────────


def _row_snapshot(conn: sqlite3.Connection, table: str, where: str = "") -> list[tuple]:
    return conn.execute(f"SELECT * FROM {table} {where} ORDER BY 1").fetchall()


def test_confirm_invalidates_older_and_writes_resolution(db: sqlite3.Connection) -> None:
    a = _pair(db)
    _write_flag_verdict(db, a, "conflicts")
    _a_id, a_hash, _b_id, b_hash = a
    pid = _enqueue(db, a)

    # Byte-identical baseline of the SOURCE events + the conflict_flag row.
    events_before = _row_snapshot(db, "events", "WHERE kind = 'remember'")
    flag_before = _row_snapshot(
        db, "interpretations", "WHERE produced_by LIKE 'conflict_resolver:v0:%'"
    )

    out = decide_correction(db, proposal_id=pid, verdict="confirm")
    assert out.status == "applied"

    # The OLDER side (A) is invalidated by a NEW append-only event.
    inv = db.execute(
        "SELECT json_extract(payload,'$.target_hash') AS t FROM events WHERE kind = 'invalidate'"
    ).fetchall()
    assert [r["t"] for r in inv] == [a_hash]
    # A resolution interpretation was appended (superseded_older). Its producer
    # encodes the PAIR key (min:max of both hashes), not event B alone.
    res = db.execute(
        "SELECT extraction FROM interpretations WHERE produced_by = ?",
        (_res_producer(a_hash, b_hash),),
    ).fetchone()
    data = json.loads(res["extraction"])
    assert data["resolution"] == "superseded_older"
    assert data["pair_key"] == pair_key_for(a_hash, b_hash)
    assert data["invalidation_event_id"] is not None
    # An observe event records the operator action (I7).
    n_obs = db.execute(
        "SELECT COUNT(*) FROM events WHERE kind='observe' "
        "AND json_extract(payload,'$.action')='resolve_conflict'"
    ).fetchone()[0]
    assert n_obs == 1

    # I2: the source events + the conflict_flag row are UNTOUCHED (byte-identical).
    assert _row_snapshot(db, "events", "WHERE kind = 'remember'") == events_before
    assert (
        _row_snapshot(db, "interpretations", "WHERE produced_by LIKE 'conflict_resolver:v0:%'")
        == flag_before
    )
    # The queue row is decided.
    assert count_pending_conflict_proposals(db) == 0


def test_retract_invalidates_newer(db: sqlite3.Connection) -> None:
    a = _pair(db)
    _a_id, a_hash, _b_id, b_hash = a
    pid = _enqueue(db, a)
    out = decide_correction(db, proposal_id=pid, verdict="retract")
    assert out.status == "applied"
    inv = db.execute(
        "SELECT json_extract(payload,'$.target_hash') AS t FROM events WHERE kind = 'invalidate'"
    ).fetchall()
    assert [r["t"] for r in inv] == [b_hash]  # NEWER side invalidated
    res = db.execute(
        "SELECT extraction FROM interpretations WHERE produced_by = ?",
        (_res_producer(a_hash, b_hash),),
    ).fetchone()
    assert json.loads(res["extraction"])["resolution"] == "superseded_newer"


def test_reject_writes_no_invalidation(db: sqlite3.Connection) -> None:
    a = _pair(db)
    _, a_hash, _, b_hash = a
    pid = _enqueue(db, a)
    out = decide_correction(db, proposal_id=pid, verdict="reject")
    assert out.status == "rejected"
    assert db.execute("SELECT COUNT(*) FROM events WHERE kind='invalidate'").fetchone()[0] == 0
    res = db.execute(
        "SELECT extraction FROM interpretations WHERE produced_by = ?",
        (_res_producer(a_hash, b_hash),),
    ).fetchone()
    assert json.loads(res["extraction"])["resolution"] == "no_conflict"


def test_decide_is_idempotent(db: sqlite3.Connection) -> None:
    a = _pair(db)
    pid = _enqueue(db, a)
    first = decide_correction(db, proposal_id=pid, verdict="confirm")
    second = decide_correction(db, proposal_id=pid, verdict="confirm")
    assert first.status == "applied"
    assert second.status == "already_decided"
    # No second invalidation appended.
    assert db.execute("SELECT COUNT(*) FROM events WHERE kind='invalidate'").fetchone()[0] == 1


def test_decide_unknown_cfl_is_not_found(db: sqlite3.Connection) -> None:
    out = decide_correction(db, proposal_id="cfl_DOESNOTEXIST", verdict="confirm")
    assert out.status == "not_found"


def test_revert_on_conflict_raises(db: sqlite3.Connection) -> None:
    a = _pair(db)
    pid = _enqueue(db, a)
    with pytest.raises(ValueError, match=r"confirm.*reject.*retract"):
        decide_correction(db, proposal_id=pid, verdict="revert")


def test_to_kind_on_conflict_raises(db: sqlite3.Connection) -> None:
    a = _pair(db)
    pid = _enqueue(db, a)
    with pytest.raises(ValueError, match="to_kind is not valid"):
        decide_correction(db, proposal_id=pid, verdict="confirm", to_kind="person")


def test_confirm_skips_duplicate_invalidation_when_loser_already_invalidated(
    db: sqlite3.Connection,
) -> None:
    from afair.agents.invalidation import write_invalidation

    a = _pair(db)
    _, a_hash, _, b_hash = a
    write_invalidation(db, target_hash=a_hash, reason="prior", origin="user")
    pid = _enqueue(db, a)
    out = decide_correction(db, proposal_id=pid, verdict="confirm")
    assert out.status == "applied"
    # Only the pre-existing invalidation exists — no duplicate appended.
    assert db.execute("SELECT COUNT(*) FROM events WHERE kind='invalidate'").fetchone()[0] == 1
    # Resolution still written, referencing the existing invalidation.
    res = db.execute(
        "SELECT extraction FROM interpretations WHERE produced_by = ?",
        (_res_producer(a_hash, b_hash),),
    ).fetchone()
    assert json.loads(res["extraction"])["resolution"] == "superseded_older"


# ── read-side: resolution attaches + count drops ─────────────────────────────


def test_read_conflicts_attaches_resolution_and_count_drops(db: sqlite3.Connection) -> None:
    a = _pair(db)
    _, a_hash, _, b_hash = a
    _write_flag_verdict(db, a, "conflicts")

    # Before decision: the flag is unresolved (resolution None).
    before = read_conflicts_batch(db, [a_hash, b_hash])
    flags_b = before[b_hash]
    assert any(flag_is_unresolved(f) for f in flags_b)
    assert all(f["resolution"] is None for f in flags_b)

    pid = _enqueue(db, a)
    decide_correction(db, proposal_id=pid, verdict="confirm")

    # After: the flag is STILL served (caveat-not-suppress) but carries its
    # resolution and no longer counts as unresolved.
    after = read_conflicts_batch(db, [a_hash, b_hash])
    flags_after = after[b_hash]
    assert flags_after  # still served
    assert all(f["resolution"] == "superseded_older" for f in flags_after)
    assert not any(flag_is_unresolved(f) for f in flags_after)


# ── pruner eligibility ───────────────────────────────────────────────────────


def test_pruner_deletes_decided_not_open(db: sqlite3.Connection, settings: Settings) -> None:
    from datetime import UTC, datetime, timedelta

    from afair.agents.pruner import Pruner

    a = _pair(db)
    pid = _enqueue(db, a)
    decide_correction(db, proposal_id=pid, verdict="confirm")
    # Backdate the decision beyond the retention window.
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    with db:
        db.execute(
            "UPDATE proposed_conflict_resolutions SET decided_at = ? WHERE id = ?", (old, pid)
        )
    # A second OPEN proposal must survive the prune.
    b_id, b_hash = _event(db, "unrelated", "2026-02-01T00:00:00+00:00")
    c_id, c_hash = _event(db, "unrelated 2", "2026-03-01T00:00:00+00:00")
    open_pid = enqueue_conflict_proposal(
        db,
        event_a_id=b_id,
        event_a_hash=b_hash,
        event_b_id=c_id,
        event_b_hash=c_hash,
        newer_hash=c_hash,
        flag_verdict="conflicts",
        reason="r",
        confidence=0.8,
        detected_by="conflict_resolver:v0",
    )

    stats = Pruner().run(db, settings)
    assert stats["decided_conflicts_deleted"] == 1
    remaining = {r["id"] for r in db.execute("SELECT id FROM proposed_conflict_resolutions")}
    assert pid not in remaining
    assert open_pid in remaining


# ── export exclusion ─────────────────────────────────────────────────────────


def test_queue_excluded_from_export(db: sqlite3.Connection) -> None:
    """The regenerable queue is NOT one of the export_route's streamed tables
    (its decisions live in the exported substrate — invalidation events +
    conflict_resolution interpretations — instead)."""
    import inspect

    from afair.mcp import export_route

    source = inspect.getsource(export_route)
    # The exported-tables tuple is a list of ("<table>", "<kind>") pairs; the
    # queue must never appear as an EXPORTED table name (only in the EXCLUDED
    # documentation comment).
    assert '("proposed_conflict_resolutions"' not in source
    # The substrate carriers it relies on ARE exported.
    assert "events" in source  # invalidation events ride the events stream
    assert "interpretations" in source  # the resolution interpretation rides it


# ── shared-event pair identity (ADR-0008 regression: the blocker) ────────────


def _shared_b_pairs(
    conn: sqlite3.Connection,
) -> tuple[tuple[str, str, str, str], tuple[str, str, str, str]]:
    """Two conflict pairs that SHARE event B: (A1, B) and (A2, B). B is the
    common older event; A1 and A2 are two distinct newer events. Enqueues a
    proposal and writes a real conflict_flag for each pair."""
    b_id, b_hash = _event(conn, "User lives in Hamburg", "2025-01-01T00:00:00+00:00")
    a1_id, a1_hash = _event(conn, "User moved to Berlin", "2026-01-01T00:00:00+00:00")
    a2_id, a2_hash = _event(conn, "User lives in Munich now", "2026-06-01T00:00:00+00:00")
    p1 = (a1_id, a1_hash, b_id, b_hash)
    p2 = (a2_id, a2_hash, b_id, b_hash)
    _write_flag_verdict(conn, p1, "conflicts")
    _write_flag_verdict(conn, p2, "conflicts")
    return p1, p2


def _enqueue_newer_a(conn: sqlite3.Connection, a: tuple[str, str, str, str]) -> str:
    """Enqueue a pair whose event A is the NEWER side (B is the shared older
    event). newer_hash must point at A so a confirm invalidates the older B."""
    a_id, a_hash, b_id, b_hash = a
    pid = enqueue_conflict_proposal(
        conn,
        event_a_id=a_id,
        event_a_hash=a_hash,
        event_b_id=b_id,
        event_b_hash=b_hash,
        newer_hash=a_hash,  # A is newer in the shared-B setup
        flag_verdict="conflicts",
        reason="location changed",
        confidence=0.9,
        detected_by="conflict_resolver:v0",
    )
    assert pid is not None
    return pid


def test_shared_event_b_pairs_do_not_bleed_or_swallow(db: sqlite3.Connection) -> None:
    """Deciding one pair sharing event B must NOT resolve/suppress the OTHER
    pair, and deciding the second must record ITS OWN verdict (not a swallowed
    idempotent no-op). This is the exact blocker the B-only producer caused."""
    p1, p2 = _shared_b_pairs(db)
    _a1_id, a1_hash, _b1_id, b_hash = p1
    _a2_id, a2_hash, _b2_id, _b_hash2 = p2
    pid1 = _enqueue_newer_a(db, p1)
    pid2 = _enqueue_newer_a(db, p2)
    assert count_pending_conflict_proposals(db) == 2

    # Decide ONLY pair 1 (confirm → newer A1 is current → invalidate older B).
    out1 = decide_correction(db, proposal_id=pid1, verdict="confirm")
    assert out1.status == "applied"

    # Pair 2 must still be OPEN + unresolved + counted.
    assert count_pending_conflict_proposals(db) == 1
    flags2 = read_conflicts_batch(db, [a2_hash])[a2_hash]
    assert flags2  # pair-2 flag surfaces on A2
    assert all(f["resolution"] is None for f in flags2), "pair 2 must NOT read as resolved"
    assert any(flag_is_unresolved(f) for f in flags2), "pair 2 is still a live conflict"

    # Pair 1's own flag IS resolved (served with its resolution).
    flags1 = read_conflicts_batch(db, [a1_hash])[a1_hash]
    assert all(f["resolution"] == "superseded_older" for f in flags1)
    assert not any(flag_is_unresolved(f) for f in flags1)

    # Now decide pair 2 with a DIFFERENT verdict (reject). It must be recorded,
    # NOT swallowed as an idempotent no-op on pair 1's resolution row.
    out2 = decide_correction(db, proposal_id=pid2, verdict="reject")
    assert out2.status == "rejected"

    # Two distinct resolution interpretations exist — one per pair, each its own.
    res1 = db.execute(
        "SELECT extraction FROM interpretations WHERE produced_by = ?",
        (_res_producer(a1_hash, b_hash),),
    ).fetchone()
    res2 = db.execute(
        "SELECT extraction FROM interpretations WHERE produced_by = ?",
        (_res_producer(a2_hash, b_hash),),
    ).fetchone()
    assert res1 is not None and res2 is not None
    assert json.loads(res1["extraction"])["resolution"] == "superseded_older"
    assert json.loads(res2["extraction"])["resolution"] == "no_conflict"
    # reject wrote no invalidation, so only pair 1's invalidation of B exists.
    inv = db.execute(
        "SELECT json_extract(payload,'$.target_hash') AS t FROM events WHERE kind='invalidate'"
    ).fetchall()
    assert [r["t"] for r in inv] == [b_hash]


def test_backfill_shared_b_one_decided_resurfaces_the_other(db: sqlite3.Connection) -> None:
    """A decided (A1,B) must NOT stop the backfill from re-surfacing an
    undecided (A2,B) — the pair-scoped _has_resolution guard."""
    p1, p2 = _shared_b_pairs(db)
    pid1 = _enqueue(db, p1)
    decide_correction(db, proposal_id=pid1, verdict="confirm")
    # Pair 2 has a flag but no proposal yet. Backfill must enqueue exactly it.
    assert count_pending_conflict_proposals(db) == 0
    made = _backfill_conflict_proposals(db, max_pairs=50)
    assert made == 1
    assert count_pending_conflict_proposals(db) == 1
    open_pair_key = read_pending_conflict_proposals(db)[0].pair_key
    assert open_pair_key == pair_key_for(p2[1], p2[3])


# ── concurrent-decide race (claim-first guard) ───────────────────────────────


def test_concurrent_confirm_and_retract_yields_exactly_one_resolution(
    tmp_path: Path,
) -> None:
    """Two concurrent decides (confirm vs retract) on the same open proposal, on
    two SEPARATE connections to the same vault, must yield exactly ONE applied
    resolution and ONE invalidated side — never both. The claim-first guarded
    UPDATE serializes them."""
    conn_a = open_db(tmp_path)
    conn_b = open_db(tmp_path)
    try:
        a = _pair(conn_a)
        pid = _enqueue(conn_a, a)
        # Both connections attempt to decide the same proposal.
        out1 = decide_correction(conn_a, proposal_id=pid, verdict="confirm")
        out2 = decide_correction(conn_b, proposal_id=pid, verdict="retract")

        statuses = sorted([out1.status, out2.status])
        # Exactly one claimed (applied), the other lost (already_decided).
        assert statuses == ["already_decided", "applied"], statuses

        # Exactly one invalidation event, one resolution interpretation.
        n_inv = conn_a.execute("SELECT COUNT(*) FROM events WHERE kind='invalidate'").fetchone()[0]
        assert n_inv == 1
        n_res = conn_a.execute(
            "SELECT COUNT(*) FROM interpretations WHERE produced_by LIKE 'conflict_resolution:v1:%'"
        ).fetchone()[0]
        assert n_res == 1
        # Exactly one observe event for the operator action.
        n_obs = conn_a.execute(
            "SELECT COUNT(*) FROM events WHERE kind='observe' "
            "AND json_extract(payload,'$.action')='resolve_conflict'"
        ).fetchone()[0]
        assert n_obs == 1
    finally:
        conn_a.close()
        conn_b.close()
