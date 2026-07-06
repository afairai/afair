"""Operator-confirmation surface for Schema-Evolver proposals (ADR-0003 Phase 5).

The Schema-Evolver (``agents/schema_evolver.py``) writes *proposals* into
``proposed_ontology_revisions`` — it never applies anything. This module is
the read-and-decide side, the exact mirror of ``substrate/corrections.py``
for the entity-audit queue:

- :func:`read_pending_ontology_proposals` surfaces open proposals so the AI
  client can raise them conversationally ("The extractor has wanted a
  'research_paper' kind 40 times — add it?");
- :func:`decide_ontology_proposal` records the operator's verdict and, on
  confirm, applies the revision through the append-only kind primitives
  (:func:`~afair.substrate.kinds.register_kind` /
  :func:`~afair.substrate.kinds.write_kind_revision` /
  :func:`~afair.substrate.entities.assign_entity_kind`).

Deciding is the ONLY place a ``proposed_ontology_revisions`` row mutates
(that table is deliberately non-substrate, no I2 trigger — a regenerable
suggestion queue). The *applied* revision stays append-only: registry rows,
``kind_revisions`` rows, and kind assignments, each anchored to an
``observe`` event (``source_event_id``) — recorded and reversible, I7.

Reversal rides the same loop: ``verdict="revert"`` on an already-applied
proposal appends the compensating revision (``restore`` after
deprecate/rename/merge/split, ``deprecate`` after ``add``) plus compensating
assignment rows where the apply reassigned entities. Nothing is ever
un-written; latest-row-wins resolution does the rest.

Riding on the frozen verb: exactly like entity corrections, this is NOT a
new MCP tool (I1 forbids that). ``decide_correction`` dispatches here on the
``ont_`` id prefix; the wire shape gains only the additive ``"revert"``
verdict value.

Invariant guardrail: ``other`` is the deterministic normalization fallback
the write path depends on (Phase 3 flattening). It can gain carve-outs but
can never itself be renamed, merged away, deprecated, or split — a proposal
attempting that is refused loudly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from . import pipeline_events as pe
from .entities import assign_entity_kind, resolve_canonical
from .events import write_event
from .kinds import (
    KIND_SLUG_RE,
    live_kind_slugs,
    register_kind,
    resolve_kind_slug,
    resolve_to_live_kind,
    write_kind_revision,
)

if TYPE_CHECKING:
    import sqlite3

# Mirrors corrections.DECIDED_BY_OPERATOR (not imported: corrections imports
# this module lazily inside decide_correction; a module-level back-import
# would make the load order fragile for no gain over one duplicated literal).
DECIDED_BY_OPERATOR = "operator"

ONTOLOGY_VERDICTS = ("confirm", "reject", "revert")
"""``retract`` (the entity-noise verdict) has no ontology meaning."""

MAX_REASSIGN_PER_APPLY = 50
"""Defensive re-cap at apply time — mirrors the Schema-Evolver's
``MAX_REASSIGN_PER_PROPOSAL`` backstop for manually inserted proposals."""

PROTECTED_FALLBACK_SLUG = "other"

APPLIED_STAGE = "schema_evolver.applied"
"""``pipeline_events.stage`` stamped on every effectful apply (ADR-0003)."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class PendingOntologyProposal(BaseModel):
    """One open Schema-Evolver proposal awaiting the operator's decision."""

    id: str
    action: str
    """'add' | 'rename' | 'merge' | 'split' | 'deprecate'."""
    subject_slug: str
    """For 'add': the PROPOSED new slug (source kind in detail.source_slug).
    For every other action: the kind being revised."""
    prompt: str
    """A human-readable yes/no question, safe to show the user verbatim."""
    evidence: str
    confidence: float
    detail: dict[str, Any]


class OntologyDecisionOutcome(BaseModel):
    """Result of deciding one ontology proposal. Field-compatible with
    ``corrections.CorrectionOutcome`` so the dispatching ``decide_correction``
    returns one uniform shape to the MCP layer."""

    proposal_id: str
    status: str
    """'applied' | 'confirmed' | 'rejected' | 'reverted' | 'not_found' |
    'already_decided' | 'not_applied'."""
    note: str


def _prompt_for(action: str, subject_slug: str, detail: dict[str, Any], evidence: str) -> str:
    if action == "add":
        label = detail.get("label") or subject_slug
        n = len(detail.get("reassign_entity_ids") or [])
        moved = f" {n} existing entities would move to it." if n else ""
        return f"Add a new '{subject_slug}' kind ({label})? {evidence}.{moved}".rstrip()
    if action == "rename":
        to_slug = detail.get("to_slug", "?")
        return f"Rename kind '{subject_slug}' to '{to_slug}'? {evidence}."
    if action == "merge":
        to_slug = detail.get("to_slug", "?")
        return f"Merge kind '{subject_slug}' into '{to_slug}'? {evidence}."
    if action == "deprecate":
        return f"Deprecate the kind '{subject_slug}'? {evidence}."
    if action == "split":
        successors = ", ".join(
            f"'{s.get('slug', '?')}'" for s in detail.get("successors", []) if isinstance(s, dict)
        )
        return f"Split kind '{subject_slug}' into {successors or '?'}? {evidence}."
    return f"Review the proposed ontology revision for '{subject_slug}'."


def read_pending_ontology_proposals(
    conn: sqlite3.Connection, *, limit: int = 20, offset: int = 0
) -> list[PendingOntologyProposal]:
    """Open proposals (``status='proposed'``), most-confident first —
    the same ordering contract as ``read_pending_corrections`` (incl. the
    ``id`` tiebreaker for stable pagination) plus the additive ``offset``."""
    rows = conn.execute(
        """
        SELECT id, action, subject_slug, detail, evidence, confidence
        FROM proposed_ontology_revisions
        WHERE status = 'proposed'
        ORDER BY confidence DESC, detected_at ASC, id ASC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    out: list[PendingOntologyProposal] = []
    for r in rows:
        try:
            detail = json.loads(r["detail"])
        except (TypeError, ValueError):
            detail = {}
        if not isinstance(detail, dict):
            detail = {}
        out.append(
            PendingOntologyProposal(
                id=r["id"],
                action=r["action"],
                subject_slug=r["subject_slug"],
                prompt=_prompt_for(r["action"], r["subject_slug"], detail, r["evidence"]),
                evidence=r["evidence"],
                confidence=r["confidence"],
                detail=detail,
            )
        )
    return out


def count_pending_ontology_proposals(conn: sqlite3.Connection) -> int:
    """True total of open ontology proposals (``status='proposed'``).

    Same contract as :func:`~afair.substrate.corrections.count_pending_corrections`
    — no LIMIT, covered by ``proposed_ontology_revisions_status_idx``.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM proposed_ontology_revisions WHERE status = 'proposed'"
    ).fetchone()
    return int(row[0])


# ── apply (confirm) ─────────────────────────────────────────────────────────


def _anchor(conn: sqlite3.Connection, *, action: str, proposal_id: str, result: str) -> str:
    """Write the ``observe`` event every downstream row anchors to (I7 —
    recorded change). Returns the event id."""
    ev = write_event(
        conn,
        origin="user",
        kind="observe",
        payload={"action": action, "subject": proposal_id, "result": result},
    )
    return ev.id


def _guard_protected(slug: str) -> None:
    if slug == PROTECTED_FALLBACK_SLUG:
        msg = (
            f"kind '{PROTECTED_FALLBACK_SLUG}' is the write path's normalization "
            "fallback and cannot be renamed, merged away, deprecated, or split"
        )
        raise ValueError(msg)


def _reassign_entities(
    conn: sqlite3.Connection,
    *,
    entity_ids: list[str],
    kind_slug: str,
    assigned_by: str,
    reason: str,
    source_event_id: str,
) -> tuple[int, int]:
    """Append one kind assignment per entity (on its live canonical).
    Returns (reassigned, skipped) — a missing entity is skipped, never fatal:
    the proposal may predate a retraction or vault surgery."""
    reassigned = 0
    skipped = 0
    for eid in entity_ids[:MAX_REASSIGN_PER_APPLY]:
        if not isinstance(eid, str) or not eid:
            skipped += 1
            continue
        try:
            target = resolve_canonical(conn, eid)
            assign_entity_kind(
                conn,
                entity_id=target,
                kind_slug=kind_slug,
                assigned_by=assigned_by,
                reason=reason,
                confidence=1.0,
                source_event_id=source_event_id,
            )
            reassigned += 1
        except ValueError:
            skipped += 1
    return reassigned, skipped


def _register_or_restore(
    conn: sqlite3.Connection,
    *,
    slug: str,
    label: str,
    description: str | None,
    created_by: str,
    reason: str,
    source_event_id: str,
) -> str:
    """Make ``slug`` a live registry kind, append-only.

    A brand-new slug gets a registry row plus an 'add' revision; a slug that
    already owns a (dead) registry row gets a 'restore' revision instead —
    the registry row is permanent, its liveness is revision state.
    """
    was_new = register_kind(
        conn,
        slug=slug,
        label=label,
        description=description,
        created_by=created_by,
        source_event_id=source_event_id,
    )
    if was_new:
        write_kind_revision(
            conn,
            action="add",
            to_slug=slug,
            revised_by=created_by,
            reason=reason,
            source_event_id=source_event_id,
        )
        return "add"
    write_kind_revision(
        conn,
        action="restore",
        from_slug=slug,
        revised_by=created_by,
        reason=reason,
        source_event_id=source_event_id,
    )
    return "restore"


def _apply_add(
    conn: sqlite3.Connection,
    *,
    slug: str,
    detail: dict[str, Any],
    decided_by: str,
    proposal_id: str,
) -> tuple[str, bool]:
    """Phase-4 'add' shape: ``subject_slug`` IS the proposed new slug; the
    source kind (if the add carves an existing one) sits in
    ``detail.source_slug``; the entities that move ride
    ``detail.reassign_entity_ids`` (sample-bound and capped at proposal
    time, re-capped here)."""
    if not KIND_SLUG_RE.match(slug):
        msg = f"proposal {proposal_id} carries an invalid kind slug {slug!r}"
        raise ValueError(msg)
    if slug in live_kind_slugs(conn):
        return (f"no-op (kind '{slug}' is already live)", False)
    reason = f"operator-confirmed ontology proposal {proposal_id}"
    ev_id = _anchor(
        conn,
        action="apply_ontology_revision",
        proposal_id=proposal_id,
        result=f"add kind '{slug}'",
    )
    _register_or_restore(
        conn,
        slug=slug,
        label=str(detail.get("label") or slug.replace("_", " ").title()),
        description=(str(detail["description"]) if detail.get("description") else None),
        created_by=decided_by,
        reason=reason,
        source_event_id=ev_id,
    )
    reassign_ids = detail.get("reassign_entity_ids")
    reassigned, skipped = _reassign_entities(
        conn,
        entity_ids=reassign_ids if isinstance(reassign_ids, list) else [],
        kind_slug=slug,
        assigned_by=decided_by,
        reason=reason,
        source_event_id=ev_id,
    )
    note = f"added kind '{slug}'; reassigned {reassigned} entities"
    if skipped:
        note += f" ({skipped} skipped)"
    pe.record(conn, event_id=ev_id, stage=APPLIED_STAGE, producer=decided_by, detail=note)
    return (note, True)


def _apply_redirect(
    conn: sqlite3.Connection,
    *,
    action: str,  # 'rename' | 'merge'
    subject_slug: str,
    detail: dict[str, Any],
    decided_by: str,
    proposal_id: str,
) -> tuple[str, bool]:
    """rename/merge: ONE revision row; the resolution chain retypes the whole
    population at read time — zero per-entity writes (ADR-0003 apply step 3)."""
    from_slug = str(detail.get("from_slug") or subject_slug)
    _guard_protected(from_slug)
    to_slug = detail.get("to_slug")
    if not isinstance(to_slug, str) or not to_slug:
        msg = f"proposal {proposal_id} ({action}) is missing detail.to_slug"
        raise ValueError(msg)
    if not KIND_SLUG_RE.match(to_slug):
        msg = f"proposal {proposal_id} carries an invalid target slug {to_slug!r}"
        raise ValueError(msg)
    if from_slug not in live_kind_slugs(conn):
        return (f"no-op (kind '{from_slug}' is not live)", False)
    if action == "merge":
        resolved_to = resolve_to_live_kind(conn, to_slug)
        if resolved_to is None:
            msg = f"proposal {proposal_id} merges into unknown kind {to_slug!r}"
            raise ValueError(msg)
        to_slug = resolved_to
    if to_slug == from_slug:
        return (f"no-op ('{from_slug}' already resolves to '{to_slug}')", False)
    reason = f"operator-confirmed ontology proposal {proposal_id}"
    ev_id = _anchor(
        conn,
        action="apply_ontology_revision",
        proposal_id=proposal_id,
        result=f"{action} kind '{from_slug}' -> '{to_slug}'",
    )
    if action == "rename" and to_slug not in live_kind_slugs(conn):
        # ADR apply step 3: insert the successor registry row if new.
        _register_or_restore(
            conn,
            slug=to_slug,
            label=str(detail.get("to_label") or to_slug.replace("_", " ").title()),
            description=(str(detail["to_description"]) if detail.get("to_description") else None),
            created_by=decided_by,
            reason=reason,
            source_event_id=ev_id,
        )
    write_kind_revision(
        conn,
        action=action,
        from_slug=from_slug,
        to_slug=to_slug,
        revised_by=decided_by,
        reason=reason,
        source_event_id=ev_id,
    )
    verb = "renamed" if action == "rename" else "merged"
    note = f"{verb} kind '{from_slug}' -> '{to_slug}' (population retypes at read time)"
    pe.record(conn, event_id=ev_id, stage=APPLIED_STAGE, producer=decided_by, detail=note)
    return (note, True)


def _apply_deprecate(
    conn: sqlite3.Connection,
    *,
    subject_slug: str,
    detail: dict[str, Any],
    decided_by: str,
    proposal_id: str,
) -> tuple[str, bool]:
    slug = str(detail.get("slug") or subject_slug)
    _guard_protected(slug)
    if slug not in live_kind_slugs(conn):
        return (f"no-op (kind '{slug}' is not live)", False)
    reason = f"operator-confirmed ontology proposal {proposal_id}"
    ev_id = _anchor(
        conn,
        action="apply_ontology_revision",
        proposal_id=proposal_id,
        result=f"deprecate kind '{slug}'",
    )
    # The registry row stays forever (history); liveness flips via this row.
    write_kind_revision(
        conn,
        action="deprecate",
        from_slug=slug,
        revised_by=decided_by,
        reason=reason,
        source_event_id=ev_id,
    )
    note = f"deprecated kind '{slug}'"
    pe.record(conn, event_id=ev_id, stage=APPLIED_STAGE, producer=decided_by, detail=note)
    return (note, True)


def _apply_split(
    conn: sqlite3.Connection,
    *,
    subject_slug: str,
    detail: dict[str, Any],
    decided_by: str,
    proposal_id: str,
) -> tuple[str, bool]:
    """ADR-0003 apply step 5: insert successor kinds, write per-entity
    assignments for the proposal's classified list, then the 'split' revision
    rows (one per successor, detail marks the default successor).

    Straggler coverage: entities of the split kind that gained no explicit
    assignment must land on the default successor. The Phase-1 resolution
    chain redirects on rename/merge rows only, so the redirect itself is
    expressed as a final 'merge' row (from_slug -> default successor); the
    per-successor 'split' rows record the fan-out for history. One 'restore'
    row reverses the whole redirect (I7).

    Expected ``detail`` shape:
        {"from_slug": ..., "successors": [{"slug", "label"?, "description"?}],
         "default_slug": ..., "assignments": {entity_id: successor_slug}}
    """
    from_slug = str(detail.get("from_slug") or subject_slug)
    _guard_protected(from_slug)
    raw_successors = detail.get("successors")
    if not isinstance(raw_successors, list) or not raw_successors:
        msg = f"proposal {proposal_id} (split) is missing detail.successors"
        raise ValueError(msg)
    successors: list[dict[str, Any]] = []
    for s in raw_successors:
        if not isinstance(s, dict) or not isinstance(s.get("slug"), str):
            msg = f"proposal {proposal_id} has a malformed successor entry"
            raise ValueError(msg)
        if not KIND_SLUG_RE.match(s["slug"]):
            msg = f"proposal {proposal_id} carries an invalid successor slug {s['slug']!r}"
            raise ValueError(msg)
        successors.append(s)
    successor_slugs = [s["slug"] for s in successors]
    default_slug = str(detail.get("default_slug") or successor_slugs[0])
    if default_slug not in successor_slugs:
        msg = f"proposal {proposal_id}: default_slug {default_slug!r} is not a successor"
        raise ValueError(msg)
    if from_slug not in live_kind_slugs(conn):
        return (f"no-op (kind '{from_slug}' is not live)", False)

    reason = f"operator-confirmed ontology proposal {proposal_id}"
    ev_id = _anchor(
        conn,
        action="apply_ontology_revision",
        proposal_id=proposal_id,
        result=f"split kind '{from_slug}' -> {successor_slugs} (default '{default_slug}')",
    )
    for s in successors:
        if s["slug"] not in live_kind_slugs(conn):
            _register_or_restore(
                conn,
                slug=s["slug"],
                label=str(s.get("label") or s["slug"].replace("_", " ").title()),
                description=(str(s["description"]) if s.get("description") else None),
                created_by=decided_by,
                reason=reason,
                source_event_id=ev_id,
            )

    # Explicit assignments for the classified list.
    assignments = detail.get("assignments")
    reassigned = 0
    skipped = 0
    if isinstance(assignments, dict):
        by_target: dict[str, list[str]] = {}
        for eid, target in assignments.items():
            if isinstance(target, str) and target in successor_slugs:
                by_target.setdefault(target, []).append(eid)
            else:
                skipped += 1
        for target, eids in sorted(by_target.items()):
            r, s_ = _reassign_entities(
                conn,
                entity_ids=sorted(eids),
                kind_slug=target,
                assigned_by=decided_by,
                reason=reason,
                source_event_id=ev_id,
            )
            reassigned += r
            skipped += s_

    # Fan-out history: one 'split' row per successor, default marked.
    for slug in successor_slugs:
        write_kind_revision(
            conn,
            action="split",
            from_slug=from_slug,
            to_slug=slug,
            detail={"default": slug == default_slug},
            revised_by=decided_by,
            reason=reason,
            source_event_id=ev_id,
        )
    # The executable straggler redirect (the primitive the resolution chain
    # follows) — written last so latest-row-wins liveness sees it.
    write_kind_revision(
        conn,
        action="merge",
        from_slug=from_slug,
        to_slug=default_slug,
        detail={"split_default_redirect": True},
        revised_by=decided_by,
        reason=f"split straggler redirect to default successor ({proposal_id})",
        source_event_id=ev_id,
    )
    note = (
        f"split kind '{from_slug}' into {successor_slugs}; "
        f"reassigned {reassigned} entities, stragglers resolve to '{default_slug}'"
    )
    if skipped:
        note += f" ({skipped} skipped)"
    pe.record(conn, event_id=ev_id, stage=APPLIED_STAGE, producer=decided_by, detail=note)
    return (note, True)


def _apply_revision(
    conn: sqlite3.Connection,
    *,
    action: str,
    subject_slug: str,
    detail: dict[str, Any],
    decided_by: str,
    proposal_id: str,
) -> tuple[str, bool]:
    """Dispatch one confirmed proposal to its apply path. Returns
    (human-readable note, effectful) — a no-op confirm (target state already
    holds) writes nothing, not even the anchor event."""
    if action == "add":
        return _apply_add(
            conn, slug=subject_slug, detail=detail, decided_by=decided_by, proposal_id=proposal_id
        )
    if action in ("rename", "merge"):
        return _apply_redirect(
            conn,
            action=action,
            subject_slug=subject_slug,
            detail=detail,
            decided_by=decided_by,
            proposal_id=proposal_id,
        )
    if action == "deprecate":
        return _apply_deprecate(
            conn,
            subject_slug=subject_slug,
            detail=detail,
            decided_by=decided_by,
            proposal_id=proposal_id,
        )
    if action == "split":
        return _apply_split(
            conn,
            subject_slug=subject_slug,
            detail=detail,
            decided_by=decided_by,
            proposal_id=proposal_id,
        )
    msg = f"unknown ontology revision action {action!r}"
    raise ValueError(msg)


# ── revert (I7 — compensating rows through the same loop) ───────────────────


def _prior_kind_for_entity(conn: sqlite3.Connection, entity_id: str, *, exclude_slug: str) -> str:
    """The kind an entity held before it was assigned ``exclude_slug``:
    its latest assignment to any OTHER slug, falling back to the immutable
    ``entities.kind`` — the same resolution order the read path uses, minus
    the assignment being reverted."""
    row = conn.execute(
        """
        SELECT kind_slug FROM entity_kind_assignments
        WHERE entity_id = ? AND kind_slug != ?
        ORDER BY assigned_at DESC, id DESC LIMIT 1
        """,
        (entity_id, exclude_slug),
    ).fetchone()
    if row is not None:
        return str(row["kind_slug"])
    row = conn.execute("SELECT kind FROM entities WHERE id = ?", (entity_id,)).fetchone()
    return str(row["kind"]) if row is not None else PROTECTED_FALLBACK_SLUG


def _revert_add(
    conn: sqlite3.Connection,
    *,
    slug: str,
    detail: dict[str, Any],
    decided_by: str,
    proposal_id: str,
) -> str:
    if slug not in live_kind_slugs(conn):
        return f"no-op (kind '{slug}' is not live; nothing to revert)"
    reason = f"operator-reverted ontology proposal {proposal_id}"
    ev_id = _anchor(
        conn,
        action="revert_ontology_revision",
        proposal_id=proposal_id,
        result=f"deprecate kind '{slug}' (revert of add)",
    )
    write_kind_revision(
        conn,
        action="deprecate",
        from_slug=slug,
        revised_by=decided_by,
        reason=reason,
        source_event_id=ev_id,
    )
    # Compensating assignments: entities the apply moved go back to the kind
    # they held before (ADR-0003 reversal: "reassignments by newer assignment
    # rows").
    reverted = 0
    reassign_ids = detail.get("reassign_entity_ids")
    for eid in (reassign_ids if isinstance(reassign_ids, list) else [])[:MAX_REASSIGN_PER_APPLY]:
        if not isinstance(eid, str) or not eid:
            continue
        try:
            target = resolve_canonical(conn, eid)
            prior = _prior_kind_for_entity(conn, target, exclude_slug=slug)
            assign_entity_kind(
                conn,
                entity_id=target,
                kind_slug=prior,
                assigned_by=decided_by,
                reason=reason,
                confidence=1.0,
                source_event_id=ev_id,
            )
            reverted += 1
        except ValueError:
            continue
    return f"reverted add: deprecated kind '{slug}', restored {reverted} entity kinds"


def _revert_restore_chain(
    conn: sqlite3.Connection,
    *,
    from_slug: str,
    action: str,
    decided_by: str,
    proposal_id: str,
) -> str:
    """rename/merge/split revert: one 'restore' row on the from-slug ends the
    redirect chain there and makes the slug live again (latest-row-wins)."""
    if resolve_kind_slug(conn, from_slug) == from_slug and from_slug in live_kind_slugs(conn):
        return f"no-op (kind '{from_slug}' is already live and unredirected)"
    ev_id = _anchor(
        conn,
        action="revert_ontology_revision",
        proposal_id=proposal_id,
        result=f"restore kind '{from_slug}' (revert of {action})",
    )
    write_kind_revision(
        conn,
        action="restore",
        from_slug=from_slug,
        revised_by=decided_by,
        reason=f"operator-reverted ontology proposal {proposal_id}",
        source_event_id=ev_id,
    )
    return f"reverted {action}: restored kind '{from_slug}'"


def _revert_split(
    conn: sqlite3.Connection,
    *,
    from_slug: str,
    detail: dict[str, Any],
    decided_by: str,
    proposal_id: str,
) -> str:
    if resolve_kind_slug(conn, from_slug) == from_slug and from_slug in live_kind_slugs(conn):
        return f"no-op (kind '{from_slug}' is already live and unredirected)"
    reason = f"operator-reverted ontology proposal {proposal_id}"
    ev_id = _anchor(
        conn,
        action="revert_ontology_revision",
        proposal_id=proposal_id,
        result=f"restore kind '{from_slug}' (revert of split)",
    )
    write_kind_revision(
        conn,
        action="restore",
        from_slug=from_slug,
        revised_by=decided_by,
        reason=reason,
        source_event_id=ev_id,
    )
    note = f"reverted split: restored kind '{from_slug}'"
    # Compensating assignments for the classified list — back to the split
    # kind, now live again (stragglers never gained a row, so the ended
    # redirect alone brings them home).
    assignments = detail.get("assignments")
    if isinstance(assignments, dict) and assignments:
        reverted, _ = _reassign_entities(
            conn,
            entity_ids=sorted(k for k in assignments if isinstance(k, str)),
            kind_slug=from_slug,
            assigned_by=decided_by,
            reason=reason,
            source_event_id=ev_id,
        )
        note += f"; restored {reverted} entity kinds"
    return note


def _revert_revision(
    conn: sqlite3.Connection,
    *,
    action: str,
    subject_slug: str,
    detail: dict[str, Any],
    decided_by: str,
    proposal_id: str,
) -> str:
    if action == "add":
        return _revert_add(
            conn, slug=subject_slug, detail=detail, decided_by=decided_by, proposal_id=proposal_id
        )
    if action in ("rename", "merge"):
        from_slug = str(detail.get("from_slug") or subject_slug)
        return _revert_restore_chain(
            conn,
            from_slug=from_slug,
            action=action,
            decided_by=decided_by,
            proposal_id=proposal_id,
        )
    if action == "deprecate":
        slug = str(detail.get("slug") or subject_slug)
        if slug in live_kind_slugs(conn):
            return f"no-op (kind '{slug}' is already live)"
        ev_id = _anchor(
            conn,
            action="revert_ontology_revision",
            proposal_id=proposal_id,
            result=f"restore kind '{slug}' (revert of deprecate)",
        )
        write_kind_revision(
            conn,
            action="restore",
            from_slug=slug,
            revised_by=decided_by,
            reason=f"operator-reverted ontology proposal {proposal_id}",
            source_event_id=ev_id,
        )
        return f"reverted deprecate: restored kind '{slug}'"
    if action == "split":
        from_slug = str(detail.get("from_slug") or subject_slug)
        return _revert_split(
            conn,
            from_slug=from_slug,
            detail=detail,
            decided_by=decided_by,
            proposal_id=proposal_id,
        )
    msg = f"unknown ontology revision action {action!r}"
    raise ValueError(msg)


# ── decide ──────────────────────────────────────────────────────────────────


def _set_status(
    conn: sqlite3.Connection, proposal_id: str, status: str, now: str, decided_by: str
) -> None:
    with conn:
        conn.execute(
            "UPDATE proposed_ontology_revisions "
            "SET status = ?, decided_at = ?, decided_by = ? WHERE id = ?",
            (status, now, decided_by, proposal_id),
        )


def decide_ontology_proposal(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    verdict: str,
    decided_by: str = DECIDED_BY_OPERATOR,
) -> OntologyDecisionOutcome:
    """Record the operator's decision on one Schema-Evolver proposal.

    ``confirm`` applies the revision through the append-only kind primitives
    (anchored to an observe event; status → 'applied', or 'confirmed' when
    the target state already held and nothing was written). ``reject``
    closes the proposal untouched (status → 'rejected'; the Schema-Evolver's
    30-day cooldown reads that). ``revert`` — only valid on an 'applied'
    proposal — appends the compensating revision (I7); the queue row keeps
    its 'applied' status because the original decision is history, and the
    reversal is itself recorded in ``kind_revisions`` plus its own anchor
    event. A second revert is a semantic no-op (latest-row-wins).

    Idempotent against a decided proposal: confirm/reject on a decided row
    reports ``already_decided``; revert on a non-applied row reports
    ``not_applied``. Same double-click safety as ``decide_correction``.
    """
    if verdict not in ONTOLOGY_VERDICTS:
        msg = (
            "verdict must be 'confirm', 'reject' or 'revert' for ontology "
            f"proposals, got {verdict!r}"
        )
        raise ValueError(msg)

    row = conn.execute(
        "SELECT action, subject_slug, detail, status FROM proposed_ontology_revisions WHERE id = ?",
        (proposal_id,),
    ).fetchone()
    if row is None:
        return OntologyDecisionOutcome(
            proposal_id=proposal_id, status="not_found", note=f"no proposal {proposal_id!r}"
        )
    now = _now_iso()

    if verdict == "reject":
        if row["status"] != "proposed":
            return OntologyDecisionOutcome(
                proposal_id=proposal_id,
                status="already_decided",
                note=f"proposal already {row['status']}",
            )
        _set_status(conn, proposal_id, "rejected", now, decided_by)
        return OntologyDecisionOutcome(
            proposal_id=proposal_id, status="rejected", note="left the ontology unchanged"
        )

    try:
        detail = json.loads(row["detail"])
    except (TypeError, ValueError) as e:
        msg = f"proposal {proposal_id} detail is not valid JSON"
        raise ValueError(msg) from e
    if not isinstance(detail, dict):
        msg = f"proposal {proposal_id} detail is not a JSON object"
        raise ValueError(msg)

    if verdict == "confirm":
        if row["status"] != "proposed":
            return OntologyDecisionOutcome(
                proposal_id=proposal_id,
                status="already_decided",
                note=f"proposal already {row['status']}",
            )
        note, effectful = _apply_revision(
            conn,
            action=row["action"],
            subject_slug=row["subject_slug"],
            detail=detail,
            decided_by=decided_by,
            proposal_id=proposal_id,
        )
        status = "applied" if effectful else "confirmed"
        _set_status(conn, proposal_id, status, now, decided_by)
        return OntologyDecisionOutcome(proposal_id=proposal_id, status=status, note=note)

    # revert — only an applied revision has anything to compensate.
    if row["status"] != "applied":
        return OntologyDecisionOutcome(
            proposal_id=proposal_id,
            status="not_applied",
            note=f"proposal is {row['status']!r}; only an applied revision can be reverted",
        )
    note = _revert_revision(
        conn,
        action=row["action"],
        subject_slug=row["subject_slug"],
        detail=detail,
        decided_by=decided_by,
        proposal_id=proposal_id,
    )
    return OntologyDecisionOutcome(proposal_id=proposal_id, status="reverted", note=note)
