"""Operator resolution of synthesis conflicts (ADR-0008).

The Conflict-Resolver (``agents/conflict_resolver.py``) flags event pairs that
contradict each other but NEVER auto-invalidates — that destructive choice is
the operator's. Until now the flags were purely informational: the Memory
Mirror surfaced them with no way to act. This module closes that loop, the
exact mirror of ``substrate/corrections.py`` / ``substrate/ontology.py`` for a
third queue:

- :func:`enqueue_conflict_proposal` — the resolver records one open proposal per
  unresolved conflict pair (anti-re-nag on ``pair_key``);
- :func:`read_pending_conflict_proposals` / :func:`count_pending_conflict_proposals`
  — surface the open queue for the dashboard + the recall pending count;
- :func:`decide_conflict_proposal` — records the operator's verdict and, where
  the decision implies a supersession, applies it through the APPEND-ONLY
  primitives (a ``write_invalidation`` event + a ``conflict_resolution``
  interpretation + an ``observe`` event). The source events and the
  ``conflict_flag`` interpretation are NEVER mutated (I2).

Deciding is the ONLY place a ``proposed_conflict_resolutions`` row mutates (that
table is deliberately non-substrate, no I2 trigger — a regenerable suggestion
queue). ``decide_correction`` dispatches here on the ``cfl_`` id prefix, so the
frozen ``recall(decide=)`` verb reaches it with no new tool (I1).

Directional framing (ADR-0008) maps the three operator intents onto the frozen
verdict enum WITHOUT widening it:

    confirm  → "the newer event is current"   → invalidate the OLDER side
    reject   → "not a conflict / both stand"  → no invalidation
    retract  → "the newer event is wrong"     → invalidate the NEWER side

``revert`` and any ``to_kind`` on a ``cfl_`` id are meaningless here and raise.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from ulid import ULID

if TYPE_CHECKING:
    import sqlite3

CONFLICT_PROPOSAL_ID_PREFIX = "cfl_"
"""``decide_correction`` dispatches to this module on this id prefix — symmetric
with the ``ont_`` ontology-queue prefix (ADR-0003 Phase 5 / ADR-0008)."""

CONFLICT_RESOLUTION_PRODUCED_BY_PREFIX = "conflict_resolution:v1"
"""Producer namespace for the append-only resolution interpretation. The full
producer encodes the PAIR — ``conflict_resolution:v1:<pair_key>`` where
``pair_key = min:max`` of the two content hashes (order-independent) — so a
resolution's identity spans the pair, not just the anchor event. This matters
because the SAME event can be event B in multiple pairs (the resolver's
anchor x linked-candidates loop): keying only on ``event_b_hash`` would collide
under the UNIQUE(event_hash, version, produced_by) constraint, so deciding one
pair would silently swallow another pair's decision and bleed its resolution
across. Encoding the pair key mirrors what the conflict_flag identity already
achieves (anchor on A, producer encodes B → the flag spans the pair)."""

CONFLICT_RESOLUTION_VERSION = 1
CONFLICT_RESOLUTION_KIND = "conflict_resolution"
"""content_type marker in the interpretation extraction blob — read by the
Mirror to attach the operator's resolution to each conflict flag."""

DECIDED_BY_OPERATOR = "operator"

# The three resolution values, mapped from the frozen verdict enum.
RESOLUTION_SUPERSEDED_OLDER = "superseded_older"
RESOLUTION_SUPERSEDED_NEWER = "superseded_newer"
RESOLUTION_NO_CONFLICT = "no_conflict"

_INVALIDATION_REASON = "operator conflict resolution (ADR-0008)"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def pair_key_for(hash_a: str, hash_b: str) -> str:
    """Deterministic ``min:max`` key for the unordered pair — the anti-re-nag
    identity, matching the resolver's ``tuple(sorted([...]))`` pair identity."""
    lo, hi = sorted((hash_a, hash_b))
    return f"{lo}:{hi}"


class PendingConflictProposal(BaseModel):
    """One open conflict-resolution proposal awaiting the operator's decision."""

    id: str
    pair_key: str
    event_a_id: str
    event_a_hash: str
    event_b_id: str
    event_b_hash: str
    newer_hash: str
    flag_verdict: str
    reason: str
    confidence: float


class ConflictResolutionOutcome(BaseModel):
    """Result of deciding one conflict proposal (mirrors CorrectionOutcome)."""

    proposal_id: str
    status: str
    """'applied' | 'rejected' | 'not_found' | 'already_decided'."""
    note: str


def pair_is_settled(conn: sqlite3.Connection, pair_key: str) -> bool:
    """True when the pair identified by ``pair_key`` must NOT be re-enqueued.

    A pair is settled when EITHER:

    - an OPEN proposal (``status='proposed'``) already exists for it — the
      operator hasn't decided yet, so a second nag would be a duplicate; OR
    - a resolution interpretation (``conflict_resolution:v1:<pair_key>``) exists —
      the operator genuinely decided it, and that decision-of-record is the
      durable proof (it survives even if the queue row is pruned).

    Deliberately NOT gated on a merely-``applied``/``rejected`` QUEUE row. The
    decide path claims the queue row (status → applied/rejected) FIRST, then
    writes the substrate resolution in a following statement (correct claim-first
    ordering for a destructive op). A crash BETWEEN the claim-commit and the
    resolution write leaves an ``applied``/``rejected`` queue row with NO
    resolution interpretation — a genuine orphan. The old ANY-status guard treated
    that orphan as "decided" and permanently blocked re-enqueue, so the pair could
    never be re-surfaced or re-decided (Fable adversarial-review finding). Anchoring
    the "already decided" test on the SUBSTRATE resolution (not the queue row's
    status) lets an orphaned pair self-heal: the next enqueue/backfill re-surfaces
    it, while a genuinely-decided pair (resolution present) stays blocked.
    """
    open_row = conn.execute(
        "SELECT 1 FROM proposed_conflict_resolutions "
        "WHERE pair_key = ? AND status = 'proposed' LIMIT 1",
        (pair_key,),
    ).fetchone()
    if open_row is not None:
        return True
    resolved = conn.execute(
        "SELECT 1 FROM interpretations WHERE produced_by = ? LIMIT 1",
        (f"{CONFLICT_RESOLUTION_PRODUCED_BY_PREFIX}:{pair_key}",),
    ).fetchone()
    return resolved is not None


def enqueue_conflict_proposal(
    conn: sqlite3.Connection,
    *,
    event_a_id: str,
    event_a_hash: str,
    event_b_id: str,
    event_b_hash: str,
    newer_hash: str,
    flag_verdict: str,
    reason: str,
    confidence: float,
    detected_by: str,
) -> str | None:
    """Enqueue one open conflict proposal, anti-re-nagged on ``pair_key``.

    Returns the new proposal id, or None when the pair is already settled
    (:func:`pair_is_settled` — an open proposal OR a substrate resolution exists).
    A pair whose queue row was claimed (status ``applied``/``rejected``) but whose
    resolution write never landed (a crash between the two) is NOT settled, so it
    self-heals: this enqueue re-surfaces it. Append-only from the queue's point of
    view — it only ever INSERTs a fresh 'proposed' row.
    """
    key = pair_key_for(event_a_hash, event_b_hash)
    if pair_is_settled(conn, key):
        return None

    proposal_id = f"{CONFLICT_PROPOSAL_ID_PREFIX}{ULID()}"
    now = _now_iso()
    with conn:
        conn.execute(
            """
            INSERT INTO proposed_conflict_resolutions (
                id, pair_key, event_a_id, event_a_hash, event_b_id, event_b_hash,
                newer_hash, flag_verdict, reason, confidence, detected_by,
                detected_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed')
            """,
            (
                proposal_id,
                key,
                event_a_id,
                event_a_hash,
                event_b_id,
                event_b_hash,
                newer_hash,
                flag_verdict,
                reason,
                float(confidence),
                detected_by,
                now,
            ),
        )
    return proposal_id


def read_pending_conflict_proposals(
    conn: sqlite3.Connection, *, limit: int = 20, offset: int = 0
) -> list[PendingConflictProposal]:
    """Open proposals (``status='proposed'``), most-confident first — the same
    ordering contract as the other two queues (incl. the ``id`` tiebreaker)."""
    rows = conn.execute(
        """
        SELECT id, pair_key, event_a_id, event_a_hash, event_b_id, event_b_hash,
               newer_hash, flag_verdict, reason, confidence
        FROM proposed_conflict_resolutions
        WHERE status = 'proposed'
        ORDER BY confidence DESC, detected_at ASC, id ASC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    return [
        PendingConflictProposal(
            id=r["id"],
            pair_key=r["pair_key"],
            event_a_id=r["event_a_id"],
            event_a_hash=r["event_a_hash"],
            event_b_id=r["event_b_id"],
            event_b_hash=r["event_b_hash"],
            newer_hash=r["newer_hash"],
            flag_verdict=r["flag_verdict"],
            reason=r["reason"],
            confidence=r["confidence"],
        )
        for r in rows
    ]


def count_pending_conflict_proposals(conn: sqlite3.Connection) -> int:
    """True total of open conflict proposals (``status='proposed'``).

    Indexed COUNT over ``proposed_conflict_resolutions_status_idx`` — safe to add
    to the universal recall nudge (not a derived scan of the conflict_flag rows).
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM proposed_conflict_resolutions WHERE status = 'proposed'"
    ).fetchone()
    return int(row[0])


def decide_conflict_proposal(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    verdict: str,
    decided_by: str = DECIDED_BY_OPERATOR,
) -> ConflictResolutionOutcome:
    """Record the operator's decision on one conflict pair; apply it where a
    supersession is implied — through APPEND-ONLY primitives only.

    ``confirm`` → the newer event is current: invalidate the OLDER side,
    resolution ``superseded_older``, status → 'applied'.
    ``reject``  → not a conflict / both stand: resolution ``no_conflict``, NO
    invalidation, status → 'rejected'.
    ``retract`` → the newer event is wrong: invalidate the NEWER side,
    resolution ``superseded_newer``, status → 'applied'.

    ``revert`` (and any other verdict) raises ValueError — it is meaningless for
    a conflict pair. Idempotent: a second decision on a decided row reports
    ``already_decided``. If the losing side is ALREADY invalidated, the duplicate
    invalidation is skipped (the resolution interpretation is still written, with
    ``invalidation_event_id`` null and a note).

    Concurrency: the decision CLAIMS the queue row first with an atomic
    ``UPDATE ... WHERE status='proposed'`` (``_claim``). Exactly one of two
    concurrent decides (a dashboard double-POST + a simultaneous
    ``recall(decide=)``) wins the claim; the loser writes NO substrate and
    returns ``already_decided`` — so a confirm-vs-retract race can never
    invalidate BOTH sides. The append-only records are written only after the
    claim wins.
    """
    if verdict not in ("confirm", "reject", "retract"):
        msg = (
            "verdict must be 'confirm', 'reject' or 'retract' for conflict "
            f"proposals, got {verdict!r}"
        )
        raise ValueError(msg)

    row = conn.execute(
        """
        SELECT event_a_id, event_a_hash, event_b_id, event_b_hash, newer_hash, status
        FROM proposed_conflict_resolutions WHERE id = ?
        """,
        (proposal_id,),
    ).fetchone()
    if row is None:
        return ConflictResolutionOutcome(
            proposal_id=proposal_id, status="not_found", note=f"no proposal {proposal_id!r}"
        )
    if row["status"] != "proposed":
        return ConflictResolutionOutcome(
            proposal_id=proposal_id,
            status="already_decided",
            note=f"proposal already {row['status']}",
        )

    newer_hash = row["newer_hash"]
    a_hash = row["event_a_hash"]
    b_hash = row["event_b_hash"]
    older_hash = a_hash if newer_hash == b_hash else b_hash

    if verdict == "reject":
        new_status, resolution, invalidate_target = "rejected", RESOLUTION_NO_CONFLICT, None
    elif verdict == "confirm":  # newer is current → invalidate older
        new_status, resolution, invalidate_target = (
            "applied",
            RESOLUTION_SUPERSEDED_OLDER,
            older_hash,
        )
    else:  # retract → newer is wrong → invalidate newer
        new_status, resolution, invalidate_target = (
            "applied",
            RESOLUTION_SUPERSEDED_NEWER,
            newer_hash,
        )

    # CLAIM FIRST (race guard): atomically flip status proposed → <target> with a
    # WHERE status='proposed' predicate, so exactly one of two concurrent decides
    # (a dashboard double-POST + a simultaneous recall(decide=)) wins. A claim
    # that updates 0 rows means another decide already won — return
    # already_decided and write NO substrate (no double invalidation). Only AFTER
    # winning the claim do we append the invalidation/resolution/observe records.
    if not _claim(conn, proposal_id, new_status, resolution=resolution, decided_by=decided_by):
        return ConflictResolutionOutcome(
            proposal_id=proposal_id,
            status="already_decided",
            note="proposal already decided by a concurrent decision",
        )

    note = _apply_resolution(
        conn,
        row=row,
        proposal_id=proposal_id,
        resolution=resolution,
        invalidate_target=invalidate_target,
        decided_by=decided_by,
    )
    if verdict == "reject":
        return ConflictResolutionOutcome(proposal_id=proposal_id, status="rejected", note=note)
    return ConflictResolutionOutcome(proposal_id=proposal_id, status="applied", note=note)


def _apply_resolution(
    conn: sqlite3.Connection,
    *,
    row: Any,
    proposal_id: str,
    resolution: str,
    invalidate_target: str | None,
    decided_by: str,
) -> str:
    """Write the append-only records for one resolution, AFTER the queue claim.

    Called only once the caller has won the atomic ``_claim`` (so a lost
    concurrent decide never reaches here). Order: (1) invalidation event (skipped
    when the loser is already invalidated or on a no-conflict), (2) the
    conflict_resolution interpretation (producer encodes the pair_key, so pairs
    sharing an event don't collide), (3) an observe event marking the operator
    action (I7). Returns a note."""
    # Function-level imports keep the substrate → agents edge lazy (mirrors the
    # ontology dispatch in corrections.py).
    from ..agents.interpretation import write_interpretation
    from ..agents.invalidation import read_invalidation, write_invalidation
    from .events import read_event_by_hash

    now = _now_iso()
    invalidation_event_id: str | None = None
    skipped_note = ""

    if invalidate_target is not None:
        already = read_invalidation(conn, invalidate_target)
        if already is not None:
            # Loser already superseded — don't append a duplicate; still record
            # the operator's resolution so the pair reads as decided.
            invalidation_event_id = already.by_event_id
            skipped_note = " (loser already invalidated; no duplicate written)"
        else:
            inv = write_invalidation(
                conn,
                target_hash=invalidate_target,
                reason=_INVALIDATION_REASON,
                origin="user",
            )
            invalidation_event_id = inv.id

    # The resolution interpretation is anchored on event B, but its PRODUCER
    # encodes the pair key (min:max of both hashes) so its identity spans the
    # pair — two pairs sharing event B get two distinct rows instead of
    # colliding on the UNIQUE(event_hash, version, produced_by) constraint.
    event_b = read_event_by_hash(conn, row["event_b_hash"])
    if event_b is None:
        # Extremely defensive — the pair was enqueued from live events. If B
        # vanished we still recorded any invalidation above; report it honestly.
        return f"resolution {resolution}: event B missing, interpretation skipped{skipped_note}"

    key = pair_key_for(row["event_a_hash"], row["event_b_hash"])
    extraction: dict[str, Any] = {
        "content_type": CONFLICT_RESOLUTION_KIND,
        "status": "success",
        "resolution": resolution,
        "pair_key": key,
        "event_a_hash": row["event_a_hash"],
        "event_a_id": row["event_a_id"],
        "event_b_hash": row["event_b_hash"],
        "event_b_id": row["event_b_id"],
        "proposal_id": proposal_id,
        "invalidation_event_id": invalidation_event_id,
        "decided_by": decided_by,
        "decided_at": now,
    }
    producer = f"{CONFLICT_RESOLUTION_PRODUCED_BY_PREFIX}:{key}"
    write_interpretation(
        conn,
        event=event_b,
        version=CONFLICT_RESOLUTION_VERSION,
        produced_by=producer,
        extraction=extraction,
    )

    # Record the operator action as an observe event (I7 — recorded + auditable).
    from .events import write_event

    write_event(
        conn,
        origin="user",
        kind="observe",
        payload={
            "content_type": "event",
            "action": "resolve_conflict",
            "subject": proposal_id,
            "result": resolution,
            "invalidation_event_id": invalidation_event_id,
            "decided_by": decided_by,
        },
    )
    return f"resolution {resolution}{skipped_note}"


def _claim(
    conn: sqlite3.Connection,
    proposal_id: str,
    status: str,
    *,
    resolution: str,
    decided_by: str,
) -> bool:
    """Atomically claim an OPEN proposal for this decision — the ONLY mutation of
    this non-substrate queue.

    Guarded by ``WHERE status='proposed'`` so exactly one of two concurrent
    decides wins (the row is the lock). Returns True when this call claimed the
    row (rowcount == 1), False when it was already decided (rowcount == 0). The
    caller writes the append-only substrate records only after a True — so a
    lost race writes nothing, and a won claim precedes the invalidation/
    resolution so a same-transaction reader never sees the row still open."""
    with conn:
        cursor = conn.execute(
            "UPDATE proposed_conflict_resolutions "
            "SET status = ?, resolution = ?, decided_at = ?, decided_by = ? "
            "WHERE id = ? AND status = 'proposed'",
            (status, resolution, _now_iso(), decided_by, proposal_id),
        )
    return (cursor.rowcount or 0) == 1
