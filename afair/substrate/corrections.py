"""Operator-confirmation surface for entity-audit proposals (ADR-0002).

The entity-audit worker (``agents/entity_audit.py``) writes *proposals* into
``proposed_corrections`` — it never applies anything. This module is the
read-and-decide side the MCP ``recall`` verb rides on:

- :func:`read_pending_corrections` surfaces open proposals so the AI client
  can raise them conversationally ("'maxime.team' is filed as a person but its
  name is a domain — re-type it to a product?");
- :func:`decide_correction` records the operator's confirm/reject and, on
  confirm, applies the correction through the existing append-only primitives
  (:func:`~afair.substrate.entities.assign_entity_kind` /
  :func:`~afair.substrate.entities.write_entity_merge`).

Deciding is the ONLY place a ``proposed_corrections`` row mutates (that table
is deliberately non-substrate, no I2 trigger). The *applied* correction stays
append-only: a retype is ONE ``entity_kind_assignments`` row (ADR-0003
Phase 2 — identity unchanged, no merge-chain surgery; a revert is just
another assignment row), a merge is a new ``entity_merges`` row, and the
confirm itself is recorded as an ``observe`` event — so the change is
recorded and reversible (I7).

Riding on the frozen verb: confirmation is NOT a new MCP tool (I1 forbids
that). It is an optional typed argument on ``recall``, exactly like the
``feedback`` signal — shipped clients that never send it keep working.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from .entities import (
    assign_entity_kind,
    record_edge_review,
    resolve_canonical,
    resolve_entity_kind,
    retract_entity,
    write_entity_merge,
)
from .events import write_event
from .kinds import ONTOLOGY_PROPOSAL_ID_PREFIX, live_kind_slugs, resolve_to_live_kind

if TYPE_CHECKING:
    import sqlite3

# The decider of record when the confirmation comes through the operator's own
# MCP client (the only path today). A future multi-actor surface can pass a
# real principal here.
DECIDED_BY_OPERATOR = "operator"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class PendingCorrection(BaseModel):
    """One open entity-audit proposal awaiting the operator's decision."""

    id: str
    kind: str
    """'retype' | 'merge'."""
    entity_id: str
    entity_name: str
    prompt: str
    """A human-readable yes/no question, safe to show the user verbatim."""
    evidence: str
    confidence: float
    detail: dict[str, Any]


class CorrectionOutcome(BaseModel):
    """Result of deciding one proposal (entity-audit or ontology)."""

    proposal_id: str
    status: str
    """'applied' | 'confirmed' | 'rejected' | 'not_found' | 'already_decided'
    — plus 'reverted' / 'not_applied' for ontology proposals (Phase 5)."""
    note: str


def _entity_name(conn: sqlite3.Connection, entity_id: str) -> str:
    row = conn.execute("SELECT canonical_name FROM entities WHERE id = ?", (entity_id,)).fetchone()
    return row["canonical_name"] if row is not None else entity_id


def _prompt_for(conn: sqlite3.Connection, kind: str, name: str, detail: dict[str, Any]) -> str:
    if kind == "retype":
        return (
            f"'{name}' is filed as a {detail['from_kind']} but reads like a "
            f"{detail['to_kind']} — re-type it?"
        )
    if kind == "merge":
        into_name = _entity_name(conn, detail["into_entity_id"])
        return f"'{name}' looks like the same entity as '{into_name}' — merge them?"
    if kind == "merge_review":
        return (
            f"The deduplicator auto-merged '{detail['from_name']}' "
            f"({detail['from_kind']}) into '{detail['into_name']}' as a "
            f"{detail['merged_kind']}. Is {detail['merged_kind']} the right kind, "
            f"or should it be something else?"
        )
    if kind == "edge_review":
        return (
            f"afair derived '{detail['subject_name']} {detail['predicate']} "
            f"{detail['object_name']}' with low confidence "
            f"({detail['confidence']:.2f}). Is that relation right?"
        )
    return f"Review the proposed correction for '{name}'."


def read_pending_corrections(
    conn: sqlite3.Connection, *, limit: int = 20
) -> list[PendingCorrection]:
    """Open proposals (``status='proposed'``), most-confident first.

    Joined to ``entities`` for the canonical name so the caller has a
    ready-to-ask question without a second lookup.
    """
    rows = conn.execute(
        """
        SELECT p.id, p.kind, p.entity_id, e.canonical_name,
               p.detail, p.evidence, p.confidence
        FROM proposed_corrections p
        JOIN entities e ON e.id = p.entity_id
        WHERE p.status = 'proposed'
        ORDER BY p.confidence DESC, p.detected_at ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out: list[PendingCorrection] = []
    for r in rows:
        detail = json.loads(r["detail"])
        out.append(
            PendingCorrection(
                id=r["id"],
                kind=r["kind"],
                entity_id=r["entity_id"],
                entity_name=r["canonical_name"],
                prompt=_prompt_for(conn, r["kind"], r["canonical_name"], detail),
                evidence=r["evidence"],
                confidence=r["confidence"],
                detail=detail,
            )
        )
    return out


def count_pending_corrections(conn: sqlite3.Connection) -> int:
    """True total of open entity-audit proposals (``status='proposed'``).

    Cheap companion to :func:`read_pending_corrections`: no JOIN, no LIMIT,
    covered by ``proposed_corrections_status_idx`` — safe to run on every
    recall so clients can nudge the operator without a full stats call.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM proposed_corrections WHERE status = 'proposed'"
    ).fetchone()
    return int(row[0])


def _apply_correction(
    conn: sqlite3.Connection,
    *,
    kind: str,
    entity_id: str,
    detail: dict[str, Any],
    decided_by: str,
    proposal_id: str,
) -> str:
    """Apply a confirmed correction through the append-only primitives.

    Returns a human-readable note describing what changed. Reuses the tested
    ``assign_entity_kind`` / ``write_entity_merge`` paths rather than inlining
    their writes: a confirm that half-commits leaves the graph in a
    MORE-correct or equal state (the proposal simply stays open and
    re-confirms cleanly), never a falsely-reported one — so the
    multi-statement window is safe here, unlike edge-review reject.
    """
    reason = f"operator-confirmed audit proposal {proposal_id}"
    if kind == "retype":
        # Record the confirm as an observe event and anchor the assignment to
        # it (entity_kind_assignments.source_event_id) — I7 (recorded change).
        ev = write_event(
            conn,
            origin="user",
            kind="observe",
            payload={
                "action": "confirm_correction",
                "subject": proposal_id,
                "result": f"retype {detail['from_kind']} -> {detail['to_kind']}",
                "name": detail["name"],
            },
        )
        # ADR-0003 Phase 2: a retype is ONE kind-assignment row on the
        # entity's live canonical — identity unchanged, no merge chain.
        target = resolve_canonical(conn, entity_id)
        current_kind = resolve_entity_kind(conn, target)
        if current_kind is None:
            return "no-op (entity not found)"
        if current_kind == detail["to_kind"]:
            return "no-op (already the target kind)"
        assign_entity_kind(
            conn,
            entity_id=target,
            kind_slug=detail["to_kind"],
            assigned_by=decided_by,
            reason=reason,
            confidence=1.0,
            source_event_id=ev.id,
        )
        return f"re-typed '{detail['name']}': {current_kind} → {detail['to_kind']}"
    if kind == "merge":
        into_id = detail["into_entity_id"]
        write_entity_merge(
            conn,
            from_entity_id=entity_id,
            into_entity_id=into_id,
            merged_by=decided_by,
            reason=reason,
            confidence=1.0,
        )
        return f"merged '{_entity_name(conn, entity_id)}' → '{_entity_name(conn, into_id)}'"
    return f"unknown correction kind {kind!r}"


def _retype_merged_entity(
    conn: sqlite3.Connection,
    *,
    detail: dict[str, Any],
    to_kind: str,
    decided_by: str,
    proposal_id: str,
) -> str:
    """Correct the kind an auto-merge picked: re-type the canonical (``into``)
    entity from the merged kind to the operator's chosen kind.

    ADR-0003 Phase 2: one kind-assignment row on the merged canonical. The
    v1-era cycle dance (invalidate the original merge before re-typing, or
    ``project → product → project`` closed a loop) is gone — kind no longer
    forks identity, so no fresh entity and no back-edge exist to cycle."""
    into_name = detail["into_name"]
    merged_kind = detail["merged_kind"]
    ev = write_event(
        conn,
        origin="user",
        kind="observe",
        payload={
            "action": "correct_merge_review",
            "subject": proposal_id,
            "result": f"retype {merged_kind} -> {to_kind}",
            "name": into_name,
        },
    )
    target = resolve_canonical(conn, detail["into_entity_id"])
    current_kind = resolve_entity_kind(conn, target)
    if current_kind is None:
        return f"no-op ('{into_name}' not found)"
    if current_kind == to_kind:
        return f"no-op ('{into_name}' already {to_kind})"
    assign_entity_kind(
        conn,
        entity_id=target,
        kind_slug=to_kind,
        assigned_by=decided_by,
        reason=f"operator-corrected merge-review proposal {proposal_id}",
        confidence=1.0,
        source_event_id=ev.id,
    )
    return f"re-typed '{into_name}': {current_kind} → {to_kind}"


def _retract_proposal_target(
    conn: sqlite3.Connection,
    *,
    kind: str,
    entity_id_: str,
    detail: dict[str, Any],
    decided_by: str,
    proposal_id: str,
) -> str:
    """Withdraw the proposal's entity as noise (not a real entity). For a
    merge_review that's the merged canonical (``into``); otherwise the
    proposal's own entity."""
    target = detail["into_entity_id"] if kind == "merge_review" else entity_id_
    name = _entity_name(conn, target)
    ev = write_event(
        conn,
        origin="user",
        kind="observe",
        payload={
            "action": "retract_entity",
            "subject": proposal_id,
            "result": "retracted noise entity",
            "entity_id": target,
            "name": name,
        },
    )
    did = retract_entity(
        conn,
        entity_id=target,
        retracted_by=decided_by,
        reason=f"operator-retracted noise (proposal {proposal_id})",
        source_event_id=ev.id,
    )
    return f"retracted '{name}'" if did else f"'{name}' was already retracted"


def _set_status(
    conn: sqlite3.Connection, proposal_id: str, status: str, now: str, decided_by: str
) -> None:
    with conn:
        conn.execute(
            "UPDATE proposed_corrections "
            "SET status = ?, decided_at = ?, decided_by = ? WHERE id = ?",
            (status, now, decided_by, proposal_id),
        )


def _decide_edge_review(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    verdict: str,
    detail: dict[str, Any],
    decided_by: str,
    now: str,
) -> CorrectionOutcome:
    """Record the operator's verdict on a derived edge (ADR-0004 C4).

    A ``confirm``/``reject`` rides the already-shipped
    :func:`~afair.substrate.entities.record_edge_review` — giving it its first
    production caller — which for a reject also writes the ``edge_invalidation``
    atomically, so the edge drops out of the live graph. The decision is
    recorded as an ``observe`` event (I7). ``retract`` is meaningless for an
    edge (the entity is real; only the RELATION is in question), so it raises.
    A stale ``edge_id`` closes the proposal so it stops blocking the queue."""
    if verdict == "retract":
        msg = (
            "retract is not meaningful for an edge-review proposal "
            "(the entities are real; only the relation is in question) — "
            "use confirm or reject"
        )
        raise ValueError(msg)
    review_verdict = "confirm" if verdict == "confirm" else "reject"
    edge_label = (
        f"{detail.get('subject_name')} {detail.get('predicate')} {detail.get('object_name')}"
    )
    try:
        record_edge_review(
            conn,
            edge_id=detail["edge_id"],
            verdict=review_verdict,
            reviewed_by=decided_by,
            reason=f"decide loop, proposal {proposal_id}",
        )
    except ValueError as exc:
        # Stale edge id (e.g. the edge row is gone). Close the proposal as
        # rejected so it never re-blocks the (kind, subject) UNIQUE slot.
        _set_status(conn, proposal_id, "rejected", now, decided_by)
        return CorrectionOutcome(proposal_id=proposal_id, status="not_found", note=str(exc))
    write_event(
        conn,
        origin="user",
        kind="observe",
        payload={
            "action": "confirm_edge" if review_verdict == "confirm" else "reject_edge",
            "subject": proposal_id,
            "result": f"{review_verdict} relation",
            "edge": edge_label,
        },
    )
    _set_status(conn, proposal_id, "applied", now, decided_by)
    return CorrectionOutcome(
        proposal_id=proposal_id,
        status="applied",
        note=f"{review_verdict}ed relation '{edge_label}'",
    )


def _decide_merge_review(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    verdict: str,
    detail: dict[str, Any],
    to_kind: str | None,
    decided_by: str,
    now: str,
) -> CorrectionOutcome:
    """A cross-kind auto-merge review. ``confirm`` keeps the picked kind (no
    change); ``reject`` with a ``to_kind`` re-types the merged entity to the
    correct kind; ``reject`` without one just flags it."""
    merged_kind = detail["merged_kind"]
    into_name = detail["into_name"]
    if verdict == "confirm":
        _set_status(conn, proposal_id, "confirmed", now, decided_by)
        return CorrectionOutcome(
            proposal_id=proposal_id,
            status="confirmed",
            note=f"kept '{into_name}' as {merged_kind}",
        )
    # reject — the auto-picked kind is wrong.
    if to_kind is not None and to_kind != merged_kind:
        note = _retype_merged_entity(
            conn, detail=detail, to_kind=to_kind, decided_by=decided_by, proposal_id=proposal_id
        )
        _set_status(conn, proposal_id, "applied", now, decided_by)
        return CorrectionOutcome(proposal_id=proposal_id, status="applied", note=note)
    _set_status(conn, proposal_id, "rejected", now, decided_by)
    return CorrectionOutcome(
        proposal_id=proposal_id,
        status="rejected",
        note="flagged as wrong kind; no target kind given, left as-is",
    )


def decide_correction(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    verdict: str,
    to_kind: str | None = None,
    decided_by: str = DECIDED_BY_OPERATOR,
) -> CorrectionOutcome:
    """Record the operator's decision on one proposal; apply it where a confirm
    (or a corrective reject) implies a change.

    ``to_kind`` carries the corrected kind for a ``merge_review`` reject ("no,
    Clario is a project, not a product"). It's validated against the kind
    registry (ADR-0003 Phase 1): the slug must resolve to a live registry
    kind — a bad value is a ValueError, not a silently-stored cast.

    Idempotent against a decided proposal: a second decision on an
    already-decided row reports the prior status, so a double-click or retry
    never double-applies.

    Ontology proposals (ADR-0003 Phase 5) ride the same loop: an id carrying
    the ``ont_`` prefix dispatches to the ontology decide path (which also
    accepts ``verdict="revert"`` on an applied revision). One verb, one
    argument, two queues — I1 holds.
    """
    if proposal_id.startswith(ONTOLOGY_PROPOSAL_ID_PREFIX):
        # Lazy import: ontology.py imports CorrectionOutcome-adjacent pieces
        # of this package; the function-level import keeps load order simple.
        from .ontology import decide_ontology_proposal

        outcome = decide_ontology_proposal(
            conn, proposal_id=proposal_id, verdict=verdict, decided_by=decided_by
        )
        return CorrectionOutcome(
            proposal_id=outcome.proposal_id, status=outcome.status, note=outcome.note
        )
    if verdict not in ("confirm", "reject", "retract"):
        msg = f"verdict must be 'confirm', 'reject' or 'retract', got {verdict!r}"
        raise ValueError(msg)
    if to_kind is not None:
        resolved_kind = resolve_to_live_kind(conn, to_kind)
        if resolved_kind is None:
            msg = f"to_kind must be one of {sorted(live_kind_slugs(conn))}, got {to_kind!r}"
            raise ValueError(msg)
        # A slug renamed/merged in the registry resolves to its live
        # successor; with no revisions (the Phase 1 state) this is identity.
        to_kind = resolved_kind

    row = conn.execute(
        "SELECT kind, entity_id, detail, status FROM proposed_corrections WHERE id = ?",
        (proposal_id,),
    ).fetchone()
    if row is None:
        return CorrectionOutcome(
            proposal_id=proposal_id, status="not_found", note=f"no proposal {proposal_id!r}"
        )
    if row["status"] != "proposed":
        return CorrectionOutcome(
            proposal_id=proposal_id,
            status="already_decided",
            note=f"proposal already {row['status']}",
        )

    detail = json.loads(row["detail"])
    now = _now_iso()

    # Edge-review proposals (ADR-0004) dispatch first: confirm/reject ride
    # record_edge_review, and retract is meaningless for a relation (handled
    # inside). This must precede the generic retract path below, which would
    # otherwise withdraw the SUBJECT entity — wrong for an edge verdict.
    if row["kind"] == "edge_review":
        return _decide_edge_review(
            conn,
            proposal_id=proposal_id,
            verdict=verdict,
            detail=detail,
            decided_by=decided_by,
            now=now,
        )

    # Retract: the proposal's entity is noise, not a real entity ("which kind?"
    # is the wrong question). Withdraw it from the live graph (append-only).
    if verdict == "retract":
        note = _retract_proposal_target(
            conn,
            kind=row["kind"],
            entity_id_=row["entity_id"],
            detail=detail,
            decided_by=decided_by,
            proposal_id=proposal_id,
        )
        _set_status(conn, proposal_id, "applied", now, decided_by)
        return CorrectionOutcome(proposal_id=proposal_id, status="applied", note=note)

    if row["kind"] == "merge_review":
        return _decide_merge_review(
            conn,
            proposal_id=proposal_id,
            verdict=verdict,
            detail=detail,
            to_kind=to_kind,
            decided_by=decided_by,
            now=now,
        )

    # retype / merge: confirm applies, reject dismisses.
    if verdict == "reject":
        _set_status(conn, proposal_id, "rejected", now, decided_by)
        return CorrectionOutcome(proposal_id=proposal_id, status="rejected", note="left as-is")

    applied_note = _apply_correction(
        conn,
        kind=row["kind"],
        entity_id=row["entity_id"],
        detail=detail,
        decided_by=decided_by,
        proposal_id=proposal_id,
    )
    _set_status(conn, proposal_id, "applied", now, decided_by)
    return CorrectionOutcome(proposal_id=proposal_id, status="applied", note=applied_note)
