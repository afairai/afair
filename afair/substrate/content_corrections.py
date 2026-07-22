"""Operator-initiated content correction (ADR-0009) — append-only writes.

The Memory Mirror lets the operator say two different things about their own
living memory:

  - **"this source is wrong"** — Flavor A / Flavor B-b1: supersede an event
    (a remembered source, or a whole synthesis) by writing a NEW
    ``invalidate`` event, optionally preceded by a NEW ``remember`` event that
    states what is actually true. The target event is NEVER mutated (I2); the
    invalidation is itself just another append-only substrate event, exactly
    like the bi-temporal supersession the cold path already emits
    (``agents/invalidation.py``). This is the ``remember(invalidates=)``
    semantics reached over the dashboard transport — NOT a proposal to confirm,
    so it does NOT route through ``decide_correction`` / ``proposed_corrections``
    (ADR-0002 single-write discipline: no phantom proposal rows).

  - **"this one key point is wrong"** — Flavor B-b2: suppress a specific served
    key point of a living synthesis WITHOUT rejecting the whole synthesis and
    WITHOUT re-running the LLM. Recorded as ONE append-only ``interpretations``
    row in a new ``key_point_review:v1:<point_digest>`` producer namespace (I3:
    a new view over unchanged substrate). Latest-wins per
    ``(event_hash, produced_by)`` via a monotonically increasing ``version``, so
    ``restore`` is a strictly-later row, never an in-place mutation. The
    synthesis payload is NEVER rewritten — the read path annotates the served
    key point with ``suppressed: true`` (ADR-0004 caveat-not-suppress:
    served WITH a marker, auditable, reversible).

This module holds ALL the SQL/substrate composition; the ``/internal/correct``
route holds zero SQL. It composes the existing substrate primitives directly on
the caller's connection (the ``import_route`` precedent), never ``handlers.remember``
(which is ServerContext-coupled).

Erasure boundary (VISION §4, ADR-0001): correction here is SUPERSESSION —
history is kept, bytes are never deleted. The only physical deletion is the
``events_fts`` index row when a synthesis is superseded (index hygiene, the
established ``living_syntheses._supersede_priors`` precedent), never a
substrate row.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from ulid import ULID

from ..agents.extractor import schedule_extraction
from ..agents.interpretation import Interpretation
from ..agents.invalidation import INVALIDATE_KIND, read_invalidation, write_invalidation
from ..agents.living_syntheses import LIVING_SYNTHESIS_KIND
from . import pipeline_events as pe
from .events import read_event_by_hash, write_event, write_event_with_status
from .payload import canonical_json

if TYPE_CHECKING:
    import sqlite3

# ── shared constants ──────────────────────────────────────────────────────
CORRECTED_BY_DASHBOARD = "operator:dashboard"
"""Provenance stamp for a correction that came through the /account dashboard.
Distinct from the MCP-client ``operator`` so an audit can tell the two apart —
mirrors ``corrections_route.DECIDED_BY_DASHBOARD``."""

OPERATOR_CORRECTION_TYPE_HINT = "operator_correction"
"""``type_hint`` on the correction remember event, so recall/extraction can tell
a "what's actually true" note apart from an ordinary imported memory."""

_DEFAULT_INVALIDATION_REASON = "operator marked wrong via Memory Mirror"

# ── Flavor B-b2 key-point review ──────────────────────────────────────────
KEY_POINT_REVIEW_PRODUCED_BY_PREFIX = "key_point_review:v1"
"""Producer namespace for the append-only key-point suppression record. The
full producer encodes the point digest —
``key_point_review:v1:<point_digest>`` — so each distinct key point of a
synthesis gets its own latest-wins lane, and a ``restore`` is a later row with
the SAME producer but a higher ``version`` (never a mutation)."""

KEY_POINT_REVIEW_KIND = "key_point_review"
"""``content_type`` marker inside the interpretation extraction blob — read by
the Mirror to annotate the matching served key point."""

VERDICT_SUPPRESS = "suppress"
VERDICT_RESTORE = "restore"

# ── Flavor B-b3 re-synthesis steering bounds (ADR-0009 Addendum 2026-07) ───
STEERING_MAX_CLAIMS = 12
"""Cap on the number of operator-marked-wrong claims fed into one re-synthesis
prompt. Bounds the fenced steering block so 12 claims x the char cap stays well
under the synthesis ``max_tokens`` budget; newest decisions win under the cap."""

STEERING_MAX_CLAIM_CHARS = 500
"""Truncation cap for a suppressed key point's text inside the steering block."""

STEERING_MAX_NOTE_CHARS = 300
"""Truncation cap for the operator's free-text note inside the steering block."""


# ── result models ─────────────────────────────────────────────────────────
class CorrectEventResult(BaseModel):
    """Outcome of correcting/superseding one event (Flavor A / B-b1)."""

    ok: bool
    target_hash: str
    already_invalidated: bool
    invalidation_event_id: str | None
    correction_event_id: str | None
    correction_content_hash: str | None
    deduplicated: bool
    fts_row_deleted: bool


class KeyPointReviewResult(BaseModel):
    """Outcome of suppressing/restoring one key point (Flavor B-b2)."""

    ok: bool
    synthesis_hash: str
    point_digest: str
    verdict: str
    status: str
    """``suppressed`` | ``restored`` | ``already_suppressed`` | ``already_restored``."""
    interpretation_id: str | None
    version: int | None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def normalize_point_text(text: str) -> str:
    """Canonical form for key-point identity — whitespace-collapsed, casefolded.

    Key points carry no stable id (they are re-derived per synthesis), so the
    operator's "mark this point wrong" is matched against the served text by a
    digest of its normalized form. Normalization tolerates incidental
    whitespace and case differences between what the dashboard rendered and
    what the operator echoed back; a genuine reword still misses (documented
    b3 limitation)."""
    return " ".join(text.split()).casefold()


def point_digest(text: str) -> str:
    """sha256 hex of the normalized key-point text — the review lane identity."""
    return hashlib.sha256(normalize_point_text(text).encode("utf-8")).hexdigest()


def correct_event(
    conn: sqlite3.Connection,
    *,
    target_hash: str,
    correction_text: str | None,
    reason: str | None,
    corrected_by: str = CORRECTED_BY_DASHBOARD,
) -> CorrectEventResult:
    """Supersede ``target_hash`` (Flavor A source-wrong / Flavor B-b1 synthesis-wrong).

    Composes the existing append-only primitives on ``conn`` in content-first
    order:

      1. IF ``correction_text``: write a NEW ``remember`` event stating what is
         actually true, ``parent_hashes=[target]`` so the supersession is
         explicit in the lineage view. On a true insert, record the pipeline
         event + schedule extraction (``suppress(RuntimeError)`` for isolated
         route tests without a ServerContext, exactly like ``import_route``).
         NOTE on re-clustering: the correction reaches the re-derived cluster via
         entity/semantic recall signals after extraction — NOT via synthesis
         lineage. ``living_syntheses._lineage_candidates`` needs the parent to be
         in ``_eligible_events``, but the parent (the target) was just
         invalidated and is therefore excluded, and the sibling bridge needs ≥2
         children. So a substantive correction re-clusters through the
         entity/semantic path; a terse one may not surface until the operator
         also corrects the source content directly.
      2. Write a NEW ``invalidate`` event marking the target no longer current
         — UNLESS the target is already invalidated (idempotent no-op).
      3. IF the target is a living synthesis: delete its ``events_fts`` row so
         the superseded synthesis stops surfacing in keyword search (index
         hygiene, the ``living_syntheses._supersede_priors`` precedent — the
         ONLY physical deletion, and it touches the index, not the substrate).
      4. Write an ``observe`` event recording the operator action (I7).

    The target event is NEVER mutated. A byte-identical correction retry is a
    content-addressed no-op (``deduplicated``); a re-invalidation of an
    already-invalid target is skipped (``already_invalidated``). Both retry
    paths return 200-shaped success — a no-op correction is success.
    """
    target = read_event_by_hash(conn, target_hash)
    if target is None:
        msg = f"correction target not found: {target_hash!r}"
        raise TargetNotFoundError(msg)
    if target.kind == INVALIDATE_KIND:
        msg = (
            f"correction target {target_hash!r} is itself an invalidation event; "
            "nested invalidations are not supported"
        )
        raise TargetIsInvalidationError(msg)

    # (1) correction remember event (content-first, before invalidation, so the
    # invalidation's lineage references a real, already-committed correction).
    correction_event_id: str | None = None
    correction_content_hash: str | None = None
    deduplicated = False
    if correction_text is not None:
        payload = {
            "content_type": "text",
            "text": correction_text,
            "context": "Operator correction via Memory Mirror",
            "type_hint": OPERATOR_CORRECTION_TYPE_HINT,
            "corrects": target_hash,
            "corrected_by": corrected_by,
        }
        # parent_hashes=[target] records the supersession explicitly in the
        # lineage view. It does NOT by itself pull the correction into the
        # re-derived cluster (the target is invalidated → excluded from
        # _eligible_events → _lineage_candidates can't bridge on it); the
        # correction re-clusters via entity/semantic signals after extraction.
        event, was_inserted = write_event_with_status(
            conn,
            origin="user",
            kind="remember",
            payload=payload,
            parent_hashes=[target_hash],
        )
        correction_event_id = event.id
        correction_content_hash = event.content_hash
        if was_inserted:
            pe.record(
                conn,
                event_id=event.id,
                event_hash=event.content_hash,
                stage=pe.STAGE_EVENT_WRITTEN,
                producer="correct:dashboard",
            )
            # Isolated route tests do not install a ServerContext; production
            # always does. The durable event is still picked up by cold-path
            # recovery either way. (import_route precedent.)
            with suppress(RuntimeError):
                schedule_extraction(event.id)
        else:
            deduplicated = True

    # (2) invalidation — skipped when the target is already superseded.
    already = read_invalidation(conn, target_hash)
    if already is not None:
        invalidation_event_id: str | None = already.by_event_id
        already_invalidated = True
    else:
        reason_text = correction_text or reason or _DEFAULT_INVALIDATION_REASON
        inv = write_invalidation(
            conn,
            target_hash=target_hash,
            reason=reason_text,
            origin="user",
        )
        invalidation_event_id = inv.id
        already_invalidated = False

    # (3) FTS index hygiene for a superseded synthesis (index, not substrate).
    fts_row_deleted = False
    if target.kind == LIVING_SYNTHESIS_KIND:
        with conn:
            conn.execute("DELETE FROM events_fts WHERE content_hash = ?", (target_hash,))
        fts_row_deleted = True

    # (4) observe (I7) — the operator action is recorded + auditable + reversible.
    result_parts = []
    result_parts.append("already_invalidated" if already_invalidated else "invalidated")
    if correction_event_id is not None:
        result_parts.append("correction_deduplicated" if deduplicated else "correction_recorded")
    write_event(
        conn,
        origin="user",
        kind="observe",
        payload={
            "content_type": "event",
            "action": "correct_content",
            "subject": target_hash,
            "result": "; ".join(result_parts),
            "invalidation_event_id": invalidation_event_id,
            "correction_event_id": correction_event_id,
            "produced_by": corrected_by,
        },
    )

    return CorrectEventResult(
        ok=True,
        target_hash=target_hash,
        already_invalidated=already_invalidated,
        invalidation_event_id=invalidation_event_id,
        correction_event_id=correction_event_id,
        correction_content_hash=correction_content_hash,
        deduplicated=deduplicated,
        fts_row_deleted=fts_row_deleted,
    )


def review_key_point(
    conn: sqlite3.Connection,
    *,
    synthesis_hash: str,
    point_text: str,
    verdict: str,
    cluster_id: str | None,
    note: str | None,
    decided_by: str = CORRECTED_BY_DASHBOARD,
) -> KeyPointReviewResult:
    """Suppress or restore one key point of a living synthesis (Flavor B-b2).

    Appends ONE ``interpretations`` row in the
    ``key_point_review:v1:<point_digest>`` producer namespace. Latest-wins per
    ``(event_hash, produced_by)`` via a monotonically increasing ``version``
    (the ``UNIQUE(event_hash, version, produced_by)`` constraint permits many
    rows with the same producer at different versions), so a ``restore`` is a
    strictly-later append — never an in-place mutation of the earlier
    ``suppress`` row. Idempotent: when the latest verdict already equals the
    requested one, no row is written.

    The caller is responsible for having validated that ``synthesis_hash`` names
    a live ``living_synthesis`` and that ``point_text`` matches a served key
    point; this function trusts those and records the decision.
    """
    if verdict not in (VERDICT_SUPPRESS, VERDICT_RESTORE):
        msg = f"verdict must be {VERDICT_SUPPRESS!r} or {VERDICT_RESTORE!r}, got {verdict!r}"
        raise ValueError(msg)

    synthesis = read_event_by_hash(conn, synthesis_hash)
    if synthesis is None:
        msg = f"key-point review target not found: {synthesis_hash!r}"
        raise TargetNotFoundError(msg)

    digest = point_digest(point_text)
    producer = f"{KEY_POINT_REVIEW_PRODUCED_BY_PREFIX}:{digest}"

    # Serialize the whole read-latest → compute-next-version → insert against
    # any concurrent decide on this same lane. ``BEGIN IMMEDIATE`` takes the
    # write lock BEFORE the read, so two concurrent suppress-vs-restore
    # POSTs (plausible with the optimistic Undo double-click) cannot both read
    # the same ``latest`` and compute the same ``version`` — the loser waits on
    # the lock (busy_timeout), then re-reads and computes a fresh version. We do
    # a direct INSERT here rather than ``write_interpretation`` so a prior
    # success row in THIS namespace does not trigger its idempotent no-op (which
    # would return a stale row and let us report a verdict that never landed).
    interp = _append_review_row(
        conn,
        synthesis=synthesis,
        producer=producer,
        digest=digest,
        point_text=point_text,
        verdict=verdict,
        cluster_id=cluster_id,
        note=note,
        decided_by=decided_by,
    )
    if interp is None:
        # No row written: the latest persisted verdict already equals the
        # requested one (idempotent no-op). Re-read to report the true state.
        latest = _read_latest_review(conn, synthesis_hash, producer)
        assert latest is not None  # a no-op only happens when a row already exists
        status = f"already_{'suppressed' if verdict == VERDICT_SUPPRESS else 'restored'}"
        return KeyPointReviewResult(
            ok=True,
            synthesis_hash=synthesis_hash,
            point_digest=digest,
            verdict=verdict,
            status=status,
            interpretation_id=latest.id,
            version=latest.version,
        )

    # Verify-persisted: confirm the row we just wrote is the CURRENT latest and
    # actually carries the requested verdict before recording the audit event.
    # A same-lane writer that committed between our BEGIN and INSERT is
    # impossible (we hold the write lock through the commit), but this guards
    # the invariant explicitly so a future refactor can't silently regress into
    # a false observe event. Only emit the observe (I7) for a decision that
    # genuinely landed as current.
    persisted = _read_latest_review(conn, synthesis_hash, producer)
    if persisted is None or persisted.extraction.get("verdict") != verdict:
        # Defensive: our write is not the current latest. Report the true
        # current state and write NO observe event (never audit a verdict that
        # is not the served truth).
        true_verdict = persisted.extraction.get("verdict") if persisted is not None else None
        status = (
            f"already_{'suppressed' if true_verdict == VERDICT_SUPPRESS else 'restored'}"
            if true_verdict in (VERDICT_SUPPRESS, VERDICT_RESTORE)
            else "superseded"
        )
        return KeyPointReviewResult(
            ok=True,
            synthesis_hash=synthesis_hash,
            point_digest=digest,
            verdict=true_verdict if isinstance(true_verdict, str) else verdict,
            status=status,
            interpretation_id=persisted.id if persisted is not None else interp.id,
            version=persisted.version if persisted is not None else interp.version,
        )

    # observe (I7) — the operator action is recorded + auditable + reversible.
    # Written only after verify-persisted confirms this verdict is current.
    write_event(
        conn,
        origin="user",
        kind="observe",
        payload={
            "content_type": "event",
            "action": "suppress_key_point",
            "subject": synthesis_hash,
            "result": verdict,
            "point_digest": digest,
            "produced_by": decided_by,
        },
    )

    status = "suppressed" if verdict == VERDICT_SUPPRESS else "restored"
    return KeyPointReviewResult(
        ok=True,
        synthesis_hash=synthesis_hash,
        point_digest=digest,
        verdict=verdict,
        status=status,
        interpretation_id=interp.id,
        version=interp.version,
    )


def _append_review_row(
    conn: sqlite3.Connection,
    *,
    synthesis: Any,
    producer: str,
    digest: str,
    point_text: str,
    verdict: str,
    cluster_id: str | None,
    note: str | None,
    decided_by: str,
) -> Interpretation | None:
    """Atomically append one key-point-review row (latest-wins by ``version``).

    Wraps read-latest → next-version → INSERT in a single ``BEGIN IMMEDIATE``
    transaction so concurrent decides on the same lane can't interleave version
    computation (the loser blocks on the write lock, then this function is
    re-entered by the caller's serialized flow with the committed row visible).
    Returns the new :class:`Interpretation`, or ``None`` when the latest row
    already carries the requested verdict (idempotent no-op — no row written).
    """
    now = _now_iso()
    extraction: dict[str, Any] = {
        "content_type": KEY_POINT_REVIEW_KIND,
        "status": "success",
        "point_digest": digest,
        "point_text": point_text,
        "verdict": verdict,
        "cluster_id": cluster_id,
        "note": note,
        "decided_by": decided_by,
        "decided_at": now,
    }
    interp_id = str(ULID())
    conn.execute("BEGIN IMMEDIATE")
    try:
        latest = _read_latest_review(conn, synthesis.content_hash, producer)
        if latest is not None and latest.extraction.get("verdict") == verdict:
            conn.execute("ROLLBACK")
            return None
        next_version = (latest.version + 1) if latest is not None else 1
        conn.execute(
            """
            INSERT INTO interpretations (
                id, event_id, event_hash, version, produced_at,
                produced_by, extraction
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interp_id,
                synthesis.id,
                synthesis.content_hash,
                next_version,
                now,
                producer,
                canonical_json(extraction),
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return Interpretation(
        id=interp_id,
        event_id=synthesis.id,
        event_hash=synthesis.content_hash,
        version=next_version,
        produced_at=now,
        produced_by=producer,
        extraction=extraction,
    )


def read_key_point_reviews(
    conn: sqlite3.Connection,
    synthesis_clusters: dict[str, str | None],
) -> dict[str, dict[str, dict[str, Any]]]:
    """Resolve the effective key-point verdict per (served synthesis, point_digest).

    Read-path helper for the Memory Mirror. ``synthesis_clusters`` maps each
    served synthesis ``content_hash`` to its ``cluster_id`` (or ``None``). The
    result is ``{synthesis_hash: {point_digest: {verdict, note, decided_at,
    cluster_id, matched_by}}}``.

    TWO lanes, because a re-derived synthesis is a NEW event with a NEW
    ``content_hash`` (``living_syntheses``: re-synthesis writes a fresh event and
    supersedes the prior), so a review keyed to the OLD synthesis hash would be
    lost the moment its synthesis re-forms:

      - **exact lane** — a review whose ``event_hash`` equals the served
        synthesis. A per-synthesis decision.
      - **cluster-fallback lane** — a review whose recorded ``cluster_id`` equals
        the served synthesis's cluster. Carries a suppression FORWARD to any
        later synthesis of the SAME cluster whose (verbatim) key point matches
        the digest. A reworded point produces a different digest and does NOT
        match (documented b3 limitation).

    Precedence: the exact lane wins over the cluster-fallback lane, so a decision
    made specifically on the re-derived synthesis overrides a carried-forward
    one. Latest-wins WITHIN each lane (highest ``version``, newest
    ``produced_at``). Projection-only — the synthesis payload is never rewritten
    (I2).
    """
    if not synthesis_clusters:
        return {}

    hashes = list(synthesis_clusters.keys())
    clusters = sorted({c for c in synthesis_clusters.values() if isinstance(c, str)})

    # One query for both lanes: rows keyed to a served synthesis hash OR carrying
    # a served cluster_id. Newest-first per lane so the first row seen per
    # (event_hash|cluster_id, point_digest) is the latest verdict.
    hash_ph = ",".join("?" * len(hashes))
    params: list[Any] = [KEY_POINT_REVIEW_PRODUCED_BY_PREFIX, *hashes]
    cluster_clause = ""
    if clusters:
        cluster_ph = ",".join("?" * len(clusters))
        cluster_clause = f" OR json_extract(extraction, '$.cluster_id') IN ({cluster_ph})"
        params.extend(clusters)
    rows = conn.execute(
        f"""
        SELECT event_hash, produced_by, version, produced_at, extraction,
               json_extract(extraction, '$.cluster_id') AS cluster_id
        FROM interpretations
        WHERE produced_by LIKE ? || ':%'
          AND (event_hash IN ({hash_ph}){cluster_clause})
        ORDER BY produced_by, version DESC, produced_at DESC
        """,
        params,
    ).fetchall()

    # Collapse to the latest verdict per exact (event_hash, produced_by) lane and
    # per (cluster_id, produced_by) lane separately.
    exact: dict[tuple[str, str], dict[str, Any]] = {}
    by_cluster: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        extraction = json.loads(row["extraction"])
        digest = extraction.get("point_digest")
        if not isinstance(digest, str):
            continue
        entry = {
            "verdict": extraction.get("verdict"),
            "note": extraction.get("note"),
            "decided_at": extraction.get("decided_at"),
            "cluster_id": extraction.get("cluster_id"),
        }
        exact_key = (row["event_hash"], digest)
        if exact_key not in exact:
            exact[exact_key] = entry  # newest-first ORDER BY → first wins
        cluster_id = row["cluster_id"]
        if isinstance(cluster_id, str):
            cluster_key = (cluster_id, digest)
            if cluster_key not in by_cluster:
                by_cluster[cluster_key] = entry

    out: dict[str, dict[str, dict[str, Any]]] = {}
    for synthesis_hash, cluster_id in synthesis_clusters.items():
        resolved: dict[str, dict[str, Any]] = {}
        # Cluster-fallback first, exact overrides — so an exact per-synthesis
        # decision wins over a carried-forward one for the same digest.
        if isinstance(cluster_id, str):
            for (c_id, digest), entry in by_cluster.items():
                if c_id == cluster_id:
                    resolved[digest] = {**entry, "matched_by": "cluster"}
        for (e_hash, digest), entry in exact.items():
            if e_hash == synthesis_hash:
                resolved[digest] = {**entry, "matched_by": "exact"}
        if resolved:
            out[synthesis_hash] = resolved
    return out


def read_live_suppressions_for_steering(
    conn: sqlite3.Connection,
    *,
    cluster_ids: list[str],
    synthesis_hashes: list[str],
    limit: int = STEERING_MAX_CLAIMS,
) -> list[dict[str, Any]]:
    """Gather the operator-marked-wrong key points to steer a re-synthesis (b3).

    Sole owner of the key-point-review lane semantics (parity with
    :func:`read_key_point_reviews`, so the write-time steering worker and the
    read-time Memory Mirror never disagree about which claims are suppressed):

      - **exact lane** — a review whose ``event_hash`` is in ``synthesis_hashes``
        (the cluster's prior synthesis events).
      - **cluster-fallback lane** — a review whose recorded ``cluster_id`` is in
        ``cluster_ids`` (the candidate's cluster plus its ancestor clusters, so a
        suppression survives a cluster merge/split).

    Precedence: exact overrides cluster per ``point_digest`` (same rule as
    :func:`read_key_point_reviews`). Latest-wins within each lane (highest
    ``version``, newest ``produced_at``). Keeps only claims whose effective
    verdict is ``suppress`` (a ``restore`` is the absence of a suppression).

    Read-only over ``interpretations`` (I2: nothing mutated, no new table).
    Returns at most ``limit`` claims, newest decision first (``decided_at``
    DESC, ``point_digest`` ASC tie-break), each ``{point_text, note|None,
    decided_at}`` with ``point_text`` truncated at :data:`STEERING_MAX_CLAIM_CHARS`
    and ``note`` at :data:`STEERING_MAX_NOTE_CHARS`.
    """
    if not cluster_ids and not synthesis_hashes:
        return []

    hashes = sorted(set(synthesis_hashes))
    clusters = sorted({c for c in cluster_ids if isinstance(c, str)})

    # One query for both lanes (same shape as read_key_point_reviews): rows keyed
    # to a prior synthesis hash OR carrying one of the candidate's cluster ids.
    # Newest-first per lane so the first row seen per lane/digest is the latest.
    where_parts: list[str] = []
    params: list[Any] = [KEY_POINT_REVIEW_PRODUCED_BY_PREFIX]
    if hashes:
        hash_ph = ",".join("?" * len(hashes))
        where_parts.append(f"event_hash IN ({hash_ph})")
        params.extend(hashes)
    if clusters:
        cluster_ph = ",".join("?" * len(clusters))
        where_parts.append(f"json_extract(extraction, '$.cluster_id') IN ({cluster_ph})")
        params.extend(clusters)
    rows = conn.execute(
        f"""
        SELECT event_hash, extraction
        FROM interpretations
        WHERE produced_by LIKE ? || ':%'
          AND ({" OR ".join(where_parts)})
        ORDER BY produced_by, version DESC, produced_at DESC
        """,
        params,
    ).fetchall()

    # Collapse to the latest verdict per exact (event_hash, digest) lane and per
    # (cluster_id, digest) lane separately (newest-first ORDER BY → first wins).
    hash_set = set(hashes)
    cluster_set = set(clusters)
    exact: dict[str, dict[str, Any]] = {}
    by_cluster: dict[str, dict[str, Any]] = {}
    for row in rows:
        extraction = json.loads(row["extraction"])
        digest = extraction.get("point_digest")
        if not isinstance(digest, str):
            continue
        entry = {
            "verdict": extraction.get("verdict"),
            "point_text": extraction.get("point_text"),
            "note": extraction.get("note"),
            "decided_at": extraction.get("decided_at"),
        }
        if row["event_hash"] in hash_set and digest not in exact:
            exact[digest] = entry
        row_cluster = extraction.get("cluster_id")
        if isinstance(row_cluster, str) and row_cluster in cluster_set and digest not in by_cluster:
            by_cluster[digest] = entry

    # Exact overrides cluster per digest (parity with read_key_point_reviews).
    resolved: dict[str, dict[str, Any]] = {**by_cluster, **exact}

    suppressed = [
        entry
        for digest, entry in resolved.items()
        if entry.get("verdict") == VERDICT_SUPPRESS
        and isinstance(entry.get("point_text"), str)
        and entry["point_text"].strip()
    ]
    # Deterministic order: decided_at DESC (newest decisions matter most under
    # the cap), point_digest ASC as the tie-break. Two stable passes give the
    # mixed direction: sort by digest ASC first, then by decided_at DESC — the
    # stable second pass preserves the digest order within equal decided_at.
    suppressed.sort(key=lambda e: point_digest(str(e["point_text"])))
    suppressed.sort(key=lambda e: e.get("decided_at") or "", reverse=True)

    out: list[dict[str, Any]] = []
    for entry in suppressed[:limit]:
        point_text = str(entry["point_text"])[:STEERING_MAX_CLAIM_CHARS]
        note_value = entry.get("note")
        note = str(note_value)[:STEERING_MAX_NOTE_CHARS] if isinstance(note_value, str) else None
        out.append(
            {
                "point_text": point_text,
                "note": note,
                "decided_at": entry.get("decided_at"),
            }
        )
    return out


def _read_latest_review(
    conn: sqlite3.Connection, event_hash: str, produced_by: str
) -> Interpretation | None:
    """Newest review row for one (event_hash, produced_by) lane, or None."""
    row = conn.execute(
        """
        SELECT * FROM interpretations
        WHERE event_hash = ? AND produced_by = ?
        ORDER BY version DESC, produced_at DESC
        LIMIT 1
        """,
        (event_hash, produced_by),
    ).fetchone()
    if row is None:
        return None
    return Interpretation(
        id=row["id"],
        event_id=row["event_id"],
        event_hash=row["event_hash"],
        version=row["version"],
        produced_at=row["produced_at"],
        produced_by=row["produced_by"],
        extraction=json.loads(row["extraction"]),
    )


# ── typed errors (surface to the route as specific HTTP codes) ────────────
class TargetNotFoundError(Exception):
    """Correction/review target hash does not name an existing event."""


class TargetIsInvalidationError(Exception):
    """Correction target is itself an ``invalidate`` event (nesting unsupported)."""
