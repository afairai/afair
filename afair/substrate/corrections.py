"""Operator-confirmation surface for entity-audit proposals (ADR-0002).

The entity-audit worker (``agents/entity_audit.py``) writes *proposals* into
``proposed_corrections`` — it never applies anything. This module is the
read-and-decide side the MCP ``recall`` verb rides on:

- :func:`read_pending_corrections` surfaces open proposals so the AI client
  can raise them conversationally ("'maxime.team' is filed as a person but its
  name is a domain — re-type it to a product?");
- :func:`decide_correction` records the operator's confirm/reject and, on
  confirm, applies the correction through the existing append-only primitives
  (:func:`~afair.substrate.entities.retype_entity` /
  :func:`~afair.substrate.entities.write_entity_merge`).

Deciding is the ONLY place a ``proposed_corrections`` row mutates (that table
is deliberately non-substrate, no I2 trigger). The *applied* correction stays
append-only: a retype is a merge into a freshly-typed entity, a merge is a new
``entity_merges`` row, and the confirm itself is recorded as an ``observe``
event — so the change is recorded and reversible (I7).

Riding on the frozen verb: confirmation is NOT a new MCP tool (I1 forbids
that). It is an optional typed argument on ``recall``, exactly like the
``feedback`` signal — shipped clients that never send it keep working.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from .entities import retype_entity, write_entity_merge
from .events import write_event

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
    """Result of deciding one proposal."""

    proposal_id: str
    status: str
    """'applied' | 'rejected' | 'not_found' | 'already_decided'."""
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
    ``retype_entity`` / ``write_entity_merge`` paths rather than inlining their
    writes: a confirm that half-commits leaves the graph in a MORE-correct or
    equal state (the proposal simply stays open and re-confirms cleanly), never
    a falsely-reported one — so the multi-statement window is safe here, unlike
    edge-review reject.
    """
    reason = f"operator-confirmed audit proposal {proposal_id}"
    if kind == "retype":
        # A new typed entity needs a real source event (entities.source_event_id
        # is a FK into events) — record the confirm as an observe event and
        # anchor the re-type to it, which also satisfies I7 (recorded change).
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
        merge = retype_entity(
            conn,
            canonical_name=detail["name"],
            from_kind=detail["from_kind"],
            to_kind=detail["to_kind"],
            reviewed_by=decided_by,
            source_event_id=ev.id,
            reason=reason,
        )
        if merge is None:
            return "no-op (entity not found or already the target kind)"
        return f"re-typed '{detail['name']}': {detail['from_kind']} → {detail['to_kind']}"
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


def decide_correction(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    verdict: str,
    decided_by: str = DECIDED_BY_OPERATOR,
) -> CorrectionOutcome:
    """Record the operator's decision on one proposal; apply it on confirm.

    Idempotent against a decided proposal: a second decision on an
    already-confirmed/rejected row is a no-op that reports the prior status,
    so a double-click or a retry never double-applies.
    """
    if verdict not in ("confirm", "reject"):
        msg = f"verdict must be 'confirm' or 'reject', got {verdict!r}"
        raise ValueError(msg)

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

    now = _now_iso()
    if verdict == "reject":
        with conn:
            conn.execute(
                "UPDATE proposed_corrections "
                "SET status = 'rejected', decided_at = ?, decided_by = ? WHERE id = ?",
                (now, decided_by, proposal_id),
            )
        return CorrectionOutcome(proposal_id=proposal_id, status="rejected", note="left as-is")

    applied_note = _apply_correction(
        conn,
        kind=row["kind"],
        entity_id=row["entity_id"],
        detail=json.loads(row["detail"]),
        decided_by=decided_by,
        proposal_id=proposal_id,
    )
    with conn:
        conn.execute(
            "UPDATE proposed_corrections "
            "SET status = 'applied', decided_at = ?, decided_by = ? WHERE id = ?",
            (now, decided_by, proposal_id),
        )
    return CorrectionOutcome(proposal_id=proposal_id, status="applied", note=applied_note)
