"""Entity-graph substrate primitives (Phase 4 Track 1).

This module owns the *table-level* surface for the five entity-graph
tables defined in ``schema.py``. The canonicalization *logic* (deciding
which surface form maps to which canonical entity) lives in
``agents/entity_canonicalizer.py``; this module only persists the
decisions.

Identity convention
-------------------
Entity IDs are content-derived. Two schemes coexist (ADR-0003 Phase 2):

    v1 (existing rows, frozen): entity:<sha256(lower(canonical_name)|kind)>
    v2 (new rows):              entity:v2:<sha256(lower(canonical_name)|disambiguator)>

v1 baked the kind into the hash, so a kind could never change without an
identity change. v2 is name-first: the ``disambiguator`` is an ordinal
that starts at "0" and increments ONLY on a deliberate homonym split
(recorded in ``entity_identities``, so the ordinal is a pure function of
prior graph state and a rebuild that replays the same canonical
decisions yields the same IDs). Existing v1 IDs are never recomputed or
rewritten; every read path handles both schemes transparently. An
entity's CURRENT kind is resolved through ``entity_kind_assignments``
(latest row wins) falling back to the immutable ``entities.kind`` — see
:func:`resolve_entity_kind_batch`. Mention / edge / merge /
invalidation / assignment rows use ULIDs for stable time-ordered IDs.

Append-only contract
--------------------
All five tables (entities, entity_mentions, entity_edges, entity_merges,
edge_invalidations) are protected by I2 triggers — no UPDATE, no DELETE.
Supersession (entity merges, edge invalidations) is recorded as
additional rows, never as in-place mutation. Reads compose the
"current view" at query time.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from ulid import ULID

from .kinds import resolve_kind_batch, resolve_kind_slug
from .sqlutil import iter_param_chunks

if TYPE_CHECKING:
    from collections.abc import Iterable


# ── identity ──────────────────────────────────────────────────────────────


ENTITY_ID_V2_PREFIX = "entity:v2:"
"""Prefix distinguishing v2 (name-first) IDs from v1 (kind-in-hash) IDs."""

ID_SCHEME_V2 = "v2"
"""``entity_identities.id_scheme`` stamp for v2 rows (v1 rows may be
backfilled lazily with 'v1'; nothing requires them)."""


def entity_id(canonical_name: str, kind: str) -> str:
    """Compute the content-derived v1 ID for a (canonical_name, kind) pair.

    Normalization: case-insensitive on the name, exact match on kind. The
    same human reference written ``Sajinth`` and ``sajinth`` both produce
    the same ID; ``Sajinth`` as person vs project produces two.

    v1 scheme — kept for reading and matching EXISTING rows (every entity
    created before ADR-0003 Phase 2 carries a v1 ID, forever). New entities
    derive their ID via :func:`entity_id_v2`.
    """
    payload = f"{canonical_name.strip().lower()}|{kind}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"entity:{digest}"


def entity_id_v2(canonical_name: str, disambiguator: str = "0") -> str:
    """Compute the content-derived v2 ID (ADR-0003 Phase 2, name-first).

    The ``disambiguator`` is an ordinal string ("0" by default) that
    increments only on a deliberate homonym split — the LLM judge (or the
    operator) ruling that a new mention of "Apple" is a different thing
    from every existing live "Apple". Kind is deliberately NOT part of the
    hash: an entity's kind can now change (one ``entity_kind_assignments``
    row) without changing its identity.
    """
    payload = f"{canonical_name.strip().lower()}|{disambiguator}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{ENTITY_ID_V2_PREFIX}{digest}"


def next_disambiguator(conn: sqlite3.Connection, canonical_name: str) -> str:
    """The ordinal the NEXT v2 identity for this name receives.

    Defined as the count of existing v2 ``entity_identities`` rows for the
    lowercased name — a pure function of prior graph state, so a rebuild
    that replays the same canonical decisions in the same event order
    reproduces the same ordinals (the determinism property v1 had).
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM entity_identities WHERE name_lower = ? AND id_scheme = ?",
        (canonical_name.strip().lower(), ID_SCHEME_V2),
    ).fetchone()
    return str(row["n"])


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_row_id() -> str:
    return str(ULID())


# ── row models ────────────────────────────────────────────────────────────


class Entity(BaseModel):
    id: str
    canonical_name: str
    kind: str
    created_at: str
    created_by: str
    confidence: float
    source_event_id: str


class EntityMention(BaseModel):
    id: str
    entity_id: str
    event_id: str
    event_hash: str
    surface_form: str
    canonicalized_at: str
    canonicalized_by: str
    match_method: str  # 'exact' | 'embedding' | 'llm' | 'new'
    confidence: float


class EntityEdge(BaseModel):
    id: str
    subject_id: str
    predicate: str
    object_id: str
    valid_from: str | None = None
    valid_to: str | None = None
    discovered_at: str
    discovered_by: str
    source_event_id: str
    confidence: float


class EntityMerge(BaseModel):
    id: str
    from_entity_id: str
    into_entity_id: str
    merged_at: str
    merged_by: str
    reason: str
    confidence: float


class EdgeInvalidation(BaseModel):
    id: str
    edge_id: str
    invalidated_at: str
    invalidated_by: str
    reason: str
    source_event_id: str | None = None


class EdgeReview(BaseModel):
    """An operator verdict on a derived edge (ADR-0002). Append-only; the
    edge's current trust state is its latest review."""

    id: str
    edge_id: str
    verdict: str  # "confirm" | "reject"
    reason: str | None
    reviewed_by: str
    reviewed_at: str


class EntityKindAssignment(BaseModel):
    """One append-only kind (re)assignment for an entity (ADR-0003 Phase 2).

    An entity's CURRENT kind is its latest assignment, falling back to the
    immutable ``entities.kind`` when no assignment exists — see
    :func:`resolve_entity_kind_batch`."""

    id: str
    entity_id: str
    kind_slug: str
    assigned_at: str
    assigned_by: str
    confidence: float
    reason: str
    source_event_id: str | None = None


# ── writes ────────────────────────────────────────────────────────────────


def write_entity(
    conn: sqlite3.Connection,
    *,
    canonical_name: str,
    kind: str,
    created_by: str,
    source_event_id: str,
    confidence: float,
    split_homonym: bool = False,
) -> Entity:
    """Insert (or return existing) a canonical entity row.

    Idempotent on (canonical_name, kind), preserving the pre-v2 contract:
    two calls with the same pair return the original row — neither the
    confidence nor source_event_id is updated, by design: per I2, the
    first creation context is preserved. The reuse checks, in order:

    1. A v1 entity with the derived ``entity_id(name, kind)`` — every row
       created before ADR-0003 Phase 2. Existing vaults keep matching their
       v1 rows; no v2 duplicate is ever created beside a v1 original.
    2. A v2 entity for this name whose *initial* kind (``entities.kind``,
       the immutable creation-time signal) equals ``kind`` — the v2
       equivalent of the v1 hash collision. Skipped when
       ``split_homonym=True``.

    With no reusable row (or on a deliberate split), a NEW v2 entity is
    created at the next disambiguator ordinal for this name, and its
    identity is recorded in ``entity_identities`` in the same transaction.
    ``split_homonym=True`` is the caller's statement that an explicit
    homonym judgment ruled this a DIFFERENT thing from EVERY existing
    same-name entity — it skips BOTH reuse checks (linking back to a
    candidate the judge just rejected would undo the split) and mints the
    next ordinal.
    """
    name_lower = canonical_name.strip().lower()
    if not split_homonym:
        v1_existing = read_entity_by_id(conn, entity_id(canonical_name, kind))
        if v1_existing is not None:
            return v1_existing
        # v2 reuse: same name + same initial kind → same entity. Initial
        # kind (not the resolved current kind) keeps the check a pure
        # function of prior creations, replay-deterministic like v1.
        row = conn.execute(
            """
            SELECT e.* FROM entity_identities i
            JOIN entities e ON e.id = i.entity_id
            WHERE i.name_lower = ? AND i.id_scheme = ? AND e.kind = ?
            ORDER BY CAST(i.disambiguator AS INTEGER) ASC LIMIT 1
            """,
            (name_lower, ID_SCHEME_V2, kind),
        ).fetchone()
        if row is not None:
            return _row_to_entity(row)

    disambiguator = next_disambiguator(conn, canonical_name)
    eid = entity_id_v2(canonical_name, disambiguator)
    created_at = _now_iso()
    # Entity row + identity row in ONE transaction — the ordinal computation
    # depends on the identity ledger being exactly as complete as the entity
    # set, so a half-commit would corrupt determinism.
    with conn:
        conn.execute(
            """
            INSERT INTO entities (
                id, canonical_name, kind, created_at, created_by,
                confidence, source_event_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (eid, canonical_name, kind, created_at, created_by, confidence, source_event_id),
        )
        conn.execute(
            """
            INSERT INTO entity_identities (
                entity_id, name_lower, disambiguator, id_scheme, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (eid, name_lower, disambiguator, ID_SCHEME_V2, created_at),
        )
    return Entity(
        id=eid,
        canonical_name=canonical_name,
        kind=kind,
        created_at=created_at,
        created_by=created_by,
        confidence=confidence,
        source_event_id=source_event_id,
    )


def write_entity_mention(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    event_id: str,
    event_hash: str,
    surface_form: str,
    canonicalized_by: str,
    match_method: str,
    confidence: float,
) -> EntityMention | None:
    """Record that this event mentions this entity via this surface form.

    Idempotent on (entity_id, event_id, surface_form). Returns the new
    row or None if the mention already existed (UNIQUE-constraint
    silently absorbs duplicates).
    """
    if match_method not in {"exact", "alias", "embedding", "llm", "new"}:
        msg = f"match_method must be one of exact/alias/embedding/llm/new, got {match_method!r}"
        raise ValueError(msg)

    row_id = _new_row_id()
    canonicalized_at = _now_iso()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO entity_mentions (
                    id, entity_id, event_id, event_hash, surface_form,
                    canonicalized_at, canonicalized_by, match_method, confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row_id,
                    entity_id,
                    event_id,
                    event_hash,
                    surface_form,
                    canonicalized_at,
                    canonicalized_by,
                    match_method,
                    confidence,
                ),
            )
    except sqlite3.IntegrityError as exc:
        # A UNIQUE-constraint violation = already linked; treat as a no-op.
        # Any other integrity error (NOT NULL / FK / CHECK) is a bug → raise.
        if "UNIQUE constraint" in str(exc):
            return None
        raise
    return EntityMention(
        id=row_id,
        entity_id=entity_id,
        event_id=event_id,
        event_hash=event_hash,
        surface_form=surface_form,
        canonicalized_at=canonicalized_at,
        canonicalized_by=canonicalized_by,
        match_method=match_method,
        confidence=confidence,
    )


def write_entity_edge(
    conn: sqlite3.Connection,
    *,
    subject_id: str,
    predicate: str,
    object_id: str,
    source_event_id: str,
    discovered_by: str,
    confidence: float,
    valid_from: str | None = None,
    valid_to: str | None = None,
) -> EntityEdge | None:
    """Record a subject-predicate-object triple discovered in a source event.

    Idempotent on (subject_id, predicate, object_id, source_event_id) —
    re-running the canonicalizer on the same event never duplicates the
    same edge from the same source. Returns None when the UNIQUE
    constraint absorbs the write.
    """
    row_id = _new_row_id()
    discovered_at = _now_iso()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO entity_edges (
                    id, subject_id, predicate, object_id,
                    valid_from, valid_to, discovered_at, discovered_by,
                    source_event_id, confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row_id,
                    subject_id,
                    predicate,
                    object_id,
                    valid_from,
                    valid_to,
                    discovered_at,
                    discovered_by,
                    source_event_id,
                    confidence,
                ),
            )
    except sqlite3.IntegrityError as exc:
        # UNIQUE violation = row already present → no-op; else propagate.
        if "UNIQUE constraint" in str(exc):
            return None
        raise
    return EntityEdge(
        id=row_id,
        subject_id=subject_id,
        predicate=predicate,
        object_id=object_id,
        valid_from=valid_from,
        valid_to=valid_to,
        discovered_at=discovered_at,
        discovered_by=discovered_by,
        source_event_id=source_event_id,
        confidence=confidence,
    )


def write_entity_merge(
    conn: sqlite3.Connection,
    *,
    from_entity_id: str,
    into_entity_id: str,
    merged_by: str,
    reason: str,
    confidence: float,
) -> EntityMerge:
    """Record that ``from_entity_id`` is the same as ``into_entity_id``.

    The "current canonical" for any entity is the transitive closure of
    entity_merges starting from that entity (see ``resolve_canonical``).

    Self-merges are rejected by the CHECK constraint at the DB level.
    Multiple merges from the same source to the same target are allowed
    (audit trail) — readers always follow the latest.
    """
    if from_entity_id == into_entity_id:
        msg = "cannot merge an entity into itself"
        raise ValueError(msg)

    row_id = _new_row_id()
    merged_at = _now_iso()
    with conn:
        conn.execute(
            """
            INSERT INTO entity_merges (
                id, from_entity_id, into_entity_id, merged_at,
                merged_by, reason, confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                from_entity_id,
                into_entity_id,
                merged_at,
                merged_by,
                reason,
                confidence,
            ),
        )
    return EntityMerge(
        id=row_id,
        from_entity_id=from_entity_id,
        into_entity_id=into_entity_id,
        merged_at=merged_at,
        merged_by=merged_by,
        reason=reason,
        confidence=confidence,
    )


def assign_entity_kind(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    kind_slug: str,
    assigned_by: str,
    reason: str,
    confidence: float = 1.0,
    source_event_id: str | None = None,
) -> EntityKindAssignment:
    """Append one kind assignment — the ADR-0003 Phase 2 retype primitive.

    The entity's identity does not change; its CURRENT kind becomes
    ``kind_slug`` at read time (latest assignment wins, see
    :func:`resolve_entity_kind_batch`). Works identically for v1 and v2
    entities — this is what replaced the v1-era merge-chain retype. A
    revert is just another assignment row (I7: recorded + reversible).
    """
    if conn.execute("SELECT 1 FROM entities WHERE id = ?", (entity_id,)).fetchone() is None:
        msg = f"entity not found: {entity_id!r}"
        raise ValueError(msg)
    row_id = _new_row_id()
    assigned_at = _now_iso()
    with conn:
        conn.execute(
            """
            INSERT INTO entity_kind_assignments (
                id, entity_id, kind_slug, assigned_at, assigned_by,
                confidence, reason, source_event_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                entity_id,
                kind_slug,
                assigned_at,
                assigned_by,
                confidence,
                reason,
                source_event_id,
            ),
        )
    return EntityKindAssignment(
        id=row_id,
        entity_id=entity_id,
        kind_slug=kind_slug,
        assigned_at=assigned_at,
        assigned_by=assigned_by,
        confidence=confidence,
        reason=reason,
        source_event_id=source_event_id,
    )


def retype_entity(
    conn: sqlite3.Connection,
    *,
    canonical_name: str,
    from_kind: str,
    to_kind: str,
    reviewed_by: str,
    source_event_id: str,
    reason: str = "operator re-typed",
) -> EntityMerge | None:
    """DEPRECATED (ADR-0003 Phase 2) — the v1-era merge-based retype.

    A v1 entity's identity encodes its kind (``entity:<sha256(name|kind)>``),
    so this path re-typed by MERGE: the old ``(name, from_kind)`` merged into
    a fresh ``(name, to_kind)`` entity, growing a merge chain per correction.
    Retype is now ONE :func:`assign_entity_kind` row — identity unchanged,
    no merge. This function stays for reading history and for tooling that
    still walks v1 vaults mid-transition; new code must not call it.

    Returns the merge row, or None if ``from_kind == to_kind`` or the source
    entity does not exist (nothing to re-type).
    """
    if from_kind == to_kind:
        return None
    source = read_entity_by_id(conn, entity_id(canonical_name, from_kind))
    if source is None:
        return None
    target = write_entity(
        conn,
        canonical_name=canonical_name,
        kind=to_kind,
        created_by=reviewed_by,
        source_event_id=source_event_id,
        confidence=1.0,
    )
    return write_entity_merge(
        conn,
        from_entity_id=source.id,
        into_entity_id=target.id,
        merged_by=reviewed_by,
        reason=reason,
        confidence=1.0,
    )


def write_edge_invalidation(
    conn: sqlite3.Connection,
    *,
    edge_id: str,
    invalidated_by: str,
    reason: str,
    source_event_id: str | None = None,
) -> EdgeInvalidation | None:
    """Mark an entity_edge as invalidated by a downstream event.

    Idempotent on (edge_id, source_event_id) — re-cascade from the same
    invalidate event is a no-op. Multiple distinct invalidate events
    against the same edge each get their own row.
    """
    row_id = _new_row_id()
    invalidated_at = _now_iso()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO edge_invalidations (
                    id, edge_id, invalidated_at, invalidated_by,
                    reason, source_event_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (row_id, edge_id, invalidated_at, invalidated_by, reason, source_event_id),
            )
    except sqlite3.IntegrityError as exc:
        # UNIQUE violation = row already present → no-op; else propagate.
        if "UNIQUE constraint" in str(exc):
            return None
        raise
    return EdgeInvalidation(
        id=row_id,
        edge_id=edge_id,
        invalidated_at=invalidated_at,
        invalidated_by=invalidated_by,
        reason=reason,
        source_event_id=source_event_id,
    )


def record_edge_serves(conn: sqlite3.Connection, edge_ids: list[str]) -> int:
    """Stamp each edge as SERVED in a recall (first time only).

    The durable signal behind the serve-gated review queue (edge_scorer only
    proposes edges that were actually surfaced to the operator) and the
    auto-expiry sweep (which keys on the ABSENCE of a row here). Append-only:
    one row per edge via ``INSERT OR IGNORE`` on the PK, so a re-serve is a
    cheap no-op and the first-served timestamp never moves. Batched under the
    host-parameter ceiling. Returns the number of NEW rows written.

    Called on the recall hot path — the caller wraps it fail-soft so a stamp
    failure never fails or meaningfully slows a recall.
    """
    if not edge_ids:
        return 0
    unique = list(dict.fromkeys(edge_ids))
    now = _now_iso()
    before = conn.total_changes
    with conn:
        for chunk in iter_param_chunks(unique):
            conn.executemany(
                "INSERT OR IGNORE INTO edge_serves (edge_id, first_served_at) VALUES (?, ?)",
                [(eid, now) for eid in chunk],
            )
    return conn.total_changes - before


def find_live_merge_from(conn: sqlite3.Connection, from_entity_id: str) -> str | None:
    """The id of the live (not-invalidated) merge out of ``from_entity_id``,
    or None. Used to reverse a merge: find it, then invalidate it."""
    row = conn.execute(
        """
        SELECT em.id FROM entity_merges em
        WHERE em.from_entity_id = ?
          AND NOT EXISTS (SELECT 1 FROM merge_invalidations mi WHERE mi.merge_id = em.id)
        ORDER BY em.merged_at DESC LIMIT 1
        """,
        (from_entity_id,),
    ).fetchone()
    return row["id"] if row is not None else None


def write_merge_invalidation(
    conn: sqlite3.Connection,
    *,
    merge_id: str,
    invalidated_by: str,
    reason: str,
    source_event_id: str | None = None,
) -> bool:
    """Mark an entity_merge as undone so ``resolve_canonical`` skips it.

    Append-only: the merge row stays as history. Idempotent on the UNIQUE
    ``merge_id`` — invalidating an already-invalidated merge returns False.
    Returns True when a new invalidation row landed.
    """
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO merge_invalidations (
                    id, merge_id, invalidated_at, invalidated_by, reason, source_event_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (_new_row_id(), merge_id, _now_iso(), invalidated_by, reason, source_event_id),
            )
    except sqlite3.IntegrityError as exc:
        # UNIQUE violation = row already present → no-op; else propagate.
        if "UNIQUE constraint" in str(exc):
            return False
        raise
    return True


def retract_entity(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    retracted_by: str,
    reason: str,
    source_event_id: str | None = None,
) -> bool:
    """Withdraw a non-entity (noise) from the live graph, append-only.

    The entity row and its mentions stay as history (I2); a retraction row
    makes every live-graph read filter it out. Idempotent on the UNIQUE
    ``entity_id`` — retracting twice returns False. Returns True on a new row.
    """
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO entity_retractions (
                    id, entity_id, retracted_at, retracted_by, reason, source_event_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (_new_row_id(), entity_id, _now_iso(), retracted_by, reason, source_event_id),
            )
    except sqlite3.IntegrityError as exc:
        # UNIQUE violation = row already present → no-op; else propagate.
        if "UNIQUE constraint" in str(exc):
            return False
        raise
    return True


def retracted_entity_ids(conn: sqlite3.Connection) -> set[str]:
    """All withdrawn entity ids — the filter set every live-graph read applies.

    Small by nature (noise is rare), so reading the whole set once per
    cold-path cycle / recall and filtering in Python beats a correlated
    subquery on every entity query.
    """
    rows = conn.execute("SELECT entity_id FROM entity_retractions").fetchall()
    return {r["entity_id"] for r in rows}


def record_edge_review(
    conn: sqlite3.Connection,
    *,
    edge_id: str,
    verdict: str,
    reviewed_by: str,
    reason: str | None = None,
    source_event_id: str | None = None,
) -> EdgeReview:
    """Append an operator verdict on a derived edge (ADR-0002).

    A ``reject`` verdict also writes an ``edge_invalidation`` so the edge drops
    out of the live graph through the existing defeasible-retraction path; the
    review row additionally records the verdict as ground truth (the signal the
    self-improvement tuner lacks). ``confirm`` only records the verdict.
    """
    if verdict not in ("confirm", "reject"):
        msg = f"verdict must be 'confirm' or 'reject', got {verdict!r}"
        raise ValueError(msg)
    # Fail with a clean domain error rather than a raw FK IntegrityError from
    # deep in the insert — the next slice resolves edge_ids from recall hits,
    # where a stale id (e.g. after a merge) is realistic input.
    if conn.execute("SELECT 1 FROM entity_edges WHERE id = ?", (edge_id,)).fetchone() is None:
        msg = f"entity_edge not found: {edge_id!r}"
        raise ValueError(msg)
    row_id = _new_row_id()
    reviewed_at = _now_iso()
    inv_id = _new_row_id() if verdict == "reject" else None
    # Both rows in ONE transaction: a reject must never half-commit a verdict
    # without its invalidation, which would leave the edge live in the graph
    # reads while latest_edge_review reports 'reject'. Raw inserts inside a
    # single `with conn:` — nesting write_edge_invalidation's own `with conn:`
    # would commit the verdict early (SQLite has no true nested transactions).
    with conn:
        conn.execute(
            """
            INSERT INTO edge_reviews (id, edge_id, verdict, reason, reviewed_by, reviewed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (row_id, edge_id, verdict, reason, reviewed_by, reviewed_at),
        )
        if verdict == "reject":
            conn.execute(
                """
                INSERT OR IGNORE INTO edge_invalidations (
                    id, edge_id, invalidated_at, invalidated_by, reason, source_event_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    inv_id,
                    edge_id,
                    reviewed_at,
                    reviewed_by,
                    reason or "operator rejected",
                    source_event_id,
                ),
            )
    return EdgeReview(
        id=row_id,
        edge_id=edge_id,
        verdict=verdict,
        reason=reason,
        reviewed_by=reviewed_by,
        reviewed_at=reviewed_at,
    )


# ── reads ─────────────────────────────────────────────────────────────────


def latest_edge_review(conn: sqlite3.Connection, edge_id: str) -> str | None:
    """The most recent verdict ('confirm'|'reject') for an edge, or None if it
    has never been reviewed.

    Ordered by ``reviewed_at`` (microsecond ISO) — the meaningful key. ``id``
    is a deterministic but otherwise arbitrary tie-break for the rare case of
    two reviews sharing the same microsecond; it is NOT a guaranteed
    write-order encoding (plain ULIDs aren't monotonic within a millisecond).
    Each ``record_edge_review`` is its own committed transaction, so in
    practice the timestamps differ.
    """
    row = conn.execute(
        "SELECT verdict FROM edge_reviews WHERE edge_id = ? "
        "ORDER BY reviewed_at DESC, id DESC LIMIT 1",
        (edge_id,),
    ).fetchone()
    return row["verdict"] if row is not None else None


def latest_edge_reviews_batch(conn: sqlite3.Connection, edge_ids: list[str]) -> dict[str, str]:
    """The latest verdict per edge for a batch of edges, in one query — avoids
    an N+1 when recall marks the trust state of every surfaced edge. Edges with
    no review are absent from the result (caller treats absence as unreviewed).
    """
    if not edge_ids:
        return {}
    out: dict[str, str] = {}
    for chunk in iter_param_chunks(edge_ids):
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT edge_id, verdict FROM edge_reviews WHERE edge_id IN ({placeholders}) "
            "ORDER BY reviewed_at ASC, id ASC",
            chunk,
        ).fetchall()
        # Ascending order means a later row overwrites an earlier one, so the
        # dict ends up holding each edge's most recent verdict. Each id is in
        # exactly one chunk, so the cross-chunk merge can't conflict.
        for row in rows:
            out[row["edge_id"]] = row["verdict"]
    return out


def read_entity_by_id(conn: sqlite3.Connection, eid: str) -> Entity | None:
    row = conn.execute("SELECT * FROM entities WHERE id = ?", (eid,)).fetchone()
    return _row_to_entity(row) if row is not None else None


def find_entity_by_name(
    conn: sqlite3.Connection,
    *,
    canonical_name: str,
    kind: str | None = None,
) -> list[Entity]:
    """Look up entities by exact canonical_name match (case-insensitive).

    The most common cheap-match step in the canonicalizer's three-stage
    pipeline. When ``kind`` is None, returns all kinds that share the
    name (rare but possible: "Apple" the company vs "apple" the concept).
    """
    if kind is not None:
        rows = conn.execute(
            """
            SELECT * FROM entities
            WHERE LOWER(canonical_name) = LOWER(?) AND kind = ?
            """,
            (canonical_name, kind),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM entities WHERE LOWER(canonical_name) = LOWER(?)",
            (canonical_name,),
        ).fetchall()
    return [_row_to_entity(r) for r in rows]


def iter_mentions_for_event(conn: sqlite3.Connection, event_hash: str) -> list[EntityMention]:
    """Read all mentions for an event. Test utility (no production call site);
    kept because the entity-graph tests assert against it."""
    rows = conn.execute(
        "SELECT * FROM entity_mentions WHERE event_hash = ? ORDER BY canonicalized_at",
        (event_hash,),
    ).fetchall()
    return [_row_to_mention(r) for r in rows]


def read_mentions_batch(
    conn: sqlite3.Connection, event_hashes: list[str]
) -> dict[str, list[EntityMention]]:
    """Batch variant — used by recall to attach mentions to many hits at once."""
    if not event_hashes:
        return {}
    placeholders = ",".join("?" for _ in event_hashes)
    rows = conn.execute(
        f"""
        SELECT * FROM entity_mentions
        WHERE event_hash IN ({placeholders})
        ORDER BY canonicalized_at
        """,
        event_hashes,
    ).fetchall()
    result: dict[str, list[EntityMention]] = {}
    for row in rows:
        result.setdefault(row["event_hash"], []).append(_row_to_mention(row))
    return result


def iter_edges_for_entity(
    conn: sqlite3.Connection, eid: str, *, include_invalidated: bool = False
) -> list[EntityEdge]:
    """Edges where this entity is either subject OR object.

    By default, edges with any edge_invalidations row are filtered out
    (decision #6: superseded entities/edges hidden by default). Pass
    ``include_invalidated=True`` to see the full historical view.
    """
    if include_invalidated:
        rows = conn.execute(
            """
            SELECT * FROM entity_edges
            WHERE subject_id = ? OR object_id = ?
            ORDER BY discovered_at DESC
            """,
            (eid, eid),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT e.* FROM entity_edges e
            LEFT JOIN edge_invalidations i ON i.edge_id = e.id
            WHERE (e.subject_id = ? OR e.object_id = ?)
              AND i.id IS NULL
            ORDER BY e.discovered_at DESC
            """,
            (eid, eid),
        ).fetchall()
    return [_row_to_edge(r) for r in rows]


def resolve_canonical(conn: sqlite3.Connection, eid: str) -> str:
    """Follow ``entity_merges`` transitively to the surviving canonical ID.

    "Sajinth" was first written as entity:A from the elvah context. Later
    canonicalizer realized it's the same Sajinth as entity:B from the
    Athara context, and wrote a merge from:A into:B. A query for entity:A
    now resolves to entity:B. If B was itself later merged into C, the
    chain follows to C. Cycle detection caps depth at 16.
    """
    current = eid
    seen: set[str] = {current}
    for _ in range(16):
        # Skip merges the operator later invalidated (a rejected auto-merge or a
        # reverted kind) — those edges are no longer part of the live graph.
        row = conn.execute(
            """
            SELECT into_entity_id FROM entity_merges em
            WHERE em.from_entity_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM merge_invalidations mi WHERE mi.merge_id = em.id
              )
            ORDER BY em.merged_at DESC LIMIT 1
            """,
            (current,),
        ).fetchone()
        if row is None:
            return current
        next_id = row["into_entity_id"]
        if next_id in seen:
            # Cycle (should never happen given CHECK constraint, but be safe).
            return current
        seen.add(next_id)
        current = next_id
    return current


def read_edge_invalidations(conn: sqlite3.Connection, edge_id: str) -> list[EdgeInvalidation]:
    """Read all invalidations for an edge. Test utility (no production call
    site); kept because the belief/edge tests assert against it."""
    rows = conn.execute(
        "SELECT * FROM edge_invalidations WHERE edge_id = ? ORDER BY invalidated_at",
        (edge_id,),
    ).fetchall()
    return [_row_to_invalidation(r) for r in rows]


def find_edges_for_source_event(conn: sqlite3.Connection, source_event_id: str) -> list[EntityEdge]:
    """All edges discovered from this source event.

    Used by the cascade-invalidation step: when a ``remember(...,
    invalidates=[hash])`` lands, find every edge whose source was the
    invalidated event and write an edge_invalidations row for each.
    """
    rows = conn.execute(
        "SELECT * FROM entity_edges WHERE source_event_id = ?",
        (source_event_id,),
    ).fetchall()
    return [_row_to_edge(r) for r in rows]


def read_edges_by_source_event_ids(
    conn: sqlite3.Connection,
    event_ids: list[str],
    *,
    include_invalidated: bool = False,
) -> dict[str, list[EntityEdge]]:
    """Batch variant — used by recall to attach edges to many hits at once.

    Filters out invalidated edges by default (decision #6) via a LEFT JOIN
    on edge_invalidations. Returns a dict keyed by source_event_id;
    event_ids with no edges are absent from the result.
    """
    if not event_ids:
        return {}
    placeholders = ",".join("?" for _ in event_ids)
    if include_invalidated:
        sql = (
            "SELECT * FROM entity_edges "
            f"WHERE source_event_id IN ({placeholders}) "
            "ORDER BY discovered_at"
        )
    else:
        sql = (
            "SELECT e.* FROM entity_edges e "
            "LEFT JOIN edge_invalidations i ON i.edge_id = e.id "
            f"WHERE e.source_event_id IN ({placeholders}) "
            "AND i.id IS NULL "
            "ORDER BY e.discovered_at"
        )
    rows = conn.execute(sql, event_ids).fetchall()
    result: dict[str, list[EntityEdge]] = {}
    for row in rows:
        result.setdefault(row["source_event_id"], []).append(_row_to_edge(row))
    return result


def count_corroborating_sources(
    conn: sqlite3.Connection,
    *,
    subject_id: str,
    predicate: str,
    object_id: str,
    exclude_event_id: str | None = None,
) -> int:
    """Count OTHER live edges asserting the same canonical triple from distinct
    source events (ADR-0004 corroboration signal).

    The ``UNIQUE(subject_id, predicate, object_id, source_event_id)`` constraint
    means an independent re-assertion of the same triple from a DIFFERENT event
    creates a sibling row — those siblings ARE the corroboration. Endpoints are
    compared MERGE-RESOLVED, so a triple re-asserted after a merge still counts
    as the same fact. Only non-invalidated (live) edges count; the edge's own
    source event is excluded via ``exclude_event_id``.
    """
    rows = conn.execute(
        """
        SELECT e.subject_id, e.object_id, e.source_event_id
        FROM entity_edges e
        LEFT JOIN edge_invalidations i ON i.edge_id = e.id
        WHERE LOWER(e.predicate) = LOWER(?)
          AND i.id IS NULL
        """,
        (predicate,),
    ).fetchall()
    if not rows:
        return 0
    ids_to_resolve = {subject_id, object_id}
    for r in rows:
        ids_to_resolve.add(r["subject_id"])
        ids_to_resolve.add(r["object_id"])
    resolved = resolve_canonical_batch(conn, list(ids_to_resolve))
    target_subj = resolved.get(subject_id, subject_id)
    target_obj = resolved.get(object_id, object_id)
    sources: set[str] = set()
    for r in rows:
        if exclude_event_id is not None and r["source_event_id"] == exclude_event_id:
            continue
        rs = resolved.get(r["subject_id"], r["subject_id"])
        ro = resolved.get(r["object_id"], r["object_id"])
        if rs == target_subj and ro == target_obj:
            sources.add(r["source_event_id"])
    return len(sources)


def count_corroborating_sources_batch(
    conn: sqlite3.Connection, edges: list[EntityEdge]
) -> dict[str, int]:
    """Corroboration count per edge for a whole batch, grouped by predicate.

    Behaviour-identical to calling :func:`count_corroborating_sources` once per
    edge (same merge-resolved endpoint comparison, same live-only + distinct-
    source semantics, each edge's own source event excluded), but issues ONE
    indexed fetch + ONE merge-resolve per DISTINCT predicate instead of per
    edge. The edge scorer used to call the single helper in a loop, so every
    scored edge full-scanned entity_edges; batching collapses a cycle's scans to
    one per predicate. Served by ``entity_edges_predicate_lower_idx``.

    Returns ``{edge_id: count}`` for every edge in the input.
    """
    if not edges:
        return {}

    by_predicate: dict[str, list[EntityEdge]] = {}
    for edge in edges:
        by_predicate.setdefault(edge.predicate, []).append(edge)

    out: dict[str, int] = {}
    for predicate, group in by_predicate.items():
        sibling_rows = conn.execute(
            """
            SELECT e.subject_id, e.object_id, e.source_event_id
            FROM entity_edges e
            LEFT JOIN edge_invalidations i ON i.edge_id = e.id
            WHERE LOWER(e.predicate) = LOWER(?)
              AND i.id IS NULL
            """,
            (predicate,),
        ).fetchall()

        ids_to_resolve: set[str] = set()
        for edge in group:
            ids_to_resolve.add(edge.subject_id)
            ids_to_resolve.add(edge.object_id)
        for r in sibling_rows:
            ids_to_resolve.add(r["subject_id"])
            ids_to_resolve.add(r["object_id"])
        resolved = resolve_canonical_batch(conn, list(ids_to_resolve))

        for edge in group:
            target_subj = resolved.get(edge.subject_id, edge.subject_id)
            target_obj = resolved.get(edge.object_id, edge.object_id)
            sources: set[str] = set()
            for r in sibling_rows:
                if r["source_event_id"] == edge.source_event_id:
                    continue
                rs = resolved.get(r["subject_id"], r["subject_id"])
                ro = resolved.get(r["object_id"], r["object_id"])
                if rs == target_subj and ro == target_obj:
                    sources.add(r["source_event_id"])
            out[edge.id] = len(sources)
    return out


def read_entities_batch(conn: sqlite3.Connection, entity_ids: Iterable[str]) -> dict[str, Entity]:
    """Bulk-fetch entities by ID. Used by recall to materialize the
    canonical_entities surface on many hits in one query.

    Accepts any iterable (list, set, dict values view); deduplicates
    internally — callers don't need to ``list(set(...))`` first.
    """
    unique_ids = list({e for e in entity_ids if e})
    if not unique_ids:
        return {}
    out: dict[str, Entity] = {}
    for chunk in iter_param_chunks(unique_ids):
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT * FROM entities WHERE id IN ({placeholders})",
            chunk,
        ).fetchall()
        for row in rows:
            out[row["id"]] = _row_to_entity(row)
    return out


def resolve_canonical_batch(conn: sqlite3.Connection, entity_ids: list[str]) -> dict[str, str]:
    """Bulk variant of resolve_canonical — one query for the whole batch.

    Uses a recursive CTE that walks the merge chain from each input ID
    until it reaches a row not in ``entity_merges.from_entity_id``. With
    a typical recall returning ~20 hits with ~3 entities each, the old
    per-id loop ran ~60 SQL queries; the CTE runs ONE (Perf audit C4).

    Depth cap matches the per-id helper (16) to defend against the
    impossible case of a merge cycle.
    """
    if not entity_ids:
        return {}

    # Deduplicate while preserving requested-ID set; non-string / falsy
    # inputs would explode the WHERE clause so filter them first.
    unique = list({e for e in entity_ids if isinstance(e, str) and e})
    if not unique:
        return {}

    # Chunk the seed so a huge id set can't exceed SQLite's host-parameter
    # limit (the VALUES seed binds one variable per id). Each id lands in
    # exactly one chunk, so merging the per-chunk result dicts is conflict-free.
    out: dict[str, str] = {}
    for chunk in iter_param_chunks(unique):
        # Build the VALUES clause for the seed; each id becomes one row.
        seed_values = ",".join("(?)" for _ in chunk)
        rows = conn.execute(
            f"""
            WITH RECURSIVE
            latest_merges(from_entity_id, into_entity_id) AS (
                -- For each from_entity_id keep only the most recent LIVE merge
                -- row. Invalidated merges (operator undid the merge) are
                -- excluded BEFORE ranking so an invalidated newer merge falls
                -- back to an older still-valid one — matching the per-id
                -- helper's NOT EXISTS + ORDER BY merged_at DESC LIMIT 1.
                SELECT from_entity_id, into_entity_id FROM (
                    SELECT
                        from_entity_id,
                        into_entity_id,
                        ROW_NUMBER() OVER (
                            PARTITION BY from_entity_id
                            ORDER BY merged_at DESC
                        ) AS rn
                    FROM entity_merges
                    WHERE NOT EXISTS (
                        SELECT 1 FROM merge_invalidations mi
                        WHERE mi.merge_id = entity_merges.id
                    )
                ) ranked
                WHERE rn = 1
            ),
            seed(id) AS (VALUES {seed_values}),
            chain(start_id, current_id, depth) AS (
                -- seed: every requested id starts as its own chain head
                SELECT id, id, 0 FROM seed
                UNION ALL
                -- step: follow from_entity_id → into_entity_id until exhausted
                SELECT chain.start_id, lm.into_entity_id, chain.depth + 1
                FROM chain
                JOIN latest_merges lm ON lm.from_entity_id = chain.current_id
                WHERE chain.depth < 16
            )
            SELECT start_id, current_id
            FROM chain AS c1
            WHERE NOT EXISTS (
                SELECT 1 FROM chain AS c2
                WHERE c2.start_id = c1.start_id
                  AND c2.depth > c1.depth
            )
            """,
            chunk,
        ).fetchall()
        # rows contains one entry per start_id whose current_id is the
        # canonical (deepest-reached) ID. IDs with no merges resolve to
        # themselves via the seed row at depth 0.
        for row in rows:
            out[row["start_id"]] = row["current_id"]
    return out


def resolve_entity_kind_batch(conn: sqlite3.Connection, entity_ids: list[str]) -> dict[str, str]:
    """The CURRENT kind slug per entity, in one query (ADR-0003 Phase 2).

    Full resolution order (the backward-compatible read path):

        entities.kind  →  latest entity_kind_assignments row (if any)
                       →  kind_revisions chain (rename/merge, latest wins)
                       →  the slug served

    Step 1+2 come from the ``entity_current_kind_v1`` view — an existing
    entity with no assignment row resolves to its stored ``entities.kind``
    byte-identically (zero backfill). Step 3 pipes the slug through the
    kind-registry revision chain so a registry-level merge (``organization``
    → ``company``) retypes every affected entity at read time with a single
    revision row and no per-entity writes. Unknown IDs are absent from the
    result (mirrors ``read_entities_batch``).
    """
    unique = list({e for e in entity_ids if isinstance(e, str) and e})
    if not unique:
        return {}
    placeholders = ",".join("?" for _ in unique)
    rows = conn.execute(
        f"SELECT entity_id, kind_slug FROM entity_current_kind_v1 "
        f"WHERE entity_id IN ({placeholders})",
        unique,
    ).fetchall()
    slugs = {row["entity_id"]: row["kind_slug"] for row in rows}
    chain = resolve_kind_batch(conn, list(slugs.values()))
    return {eid: chain.get(slug, slug) for eid, slug in slugs.items()}


def resolve_entity_kind(conn: sqlite3.Connection, eid: str) -> str | None:
    """Single-entity variant of :func:`resolve_entity_kind_batch`.

    Returns None when the entity does not exist. Prefer the batch helper on
    hot paths (recall) — this one is for corrections and tests.
    """
    row = conn.execute(
        "SELECT kind_slug FROM entity_current_kind_v1 WHERE entity_id = ?",
        (eid,),
    ).fetchone()
    if row is None:
        return None
    return resolve_kind_slug(conn, row["kind_slug"])


# ── row mappers ───────────────────────────────────────────────────────────


def _row_to_entity(row: Any) -> Entity:
    return Entity(
        id=row["id"],
        canonical_name=row["canonical_name"],
        kind=row["kind"],
        created_at=row["created_at"],
        created_by=row["created_by"],
        confidence=float(row["confidence"]),
        source_event_id=row["source_event_id"],
    )


def _row_to_mention(row: Any) -> EntityMention:
    return EntityMention(
        id=row["id"],
        entity_id=row["entity_id"],
        event_id=row["event_id"],
        event_hash=row["event_hash"],
        surface_form=row["surface_form"],
        canonicalized_at=row["canonicalized_at"],
        canonicalized_by=row["canonicalized_by"],
        match_method=row["match_method"],
        confidence=float(row["confidence"]),
    )


def _row_to_edge(row: Any) -> EntityEdge:
    return EntityEdge(
        id=row["id"],
        subject_id=row["subject_id"],
        predicate=row["predicate"],
        object_id=row["object_id"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        discovered_at=row["discovered_at"],
        discovered_by=row["discovered_by"],
        source_event_id=row["source_event_id"],
        confidence=float(row["confidence"]),
    )


def _row_to_invalidation(row: Any) -> EdgeInvalidation:
    return EdgeInvalidation(
        id=row["id"],
        edge_id=row["edge_id"],
        invalidated_at=row["invalidated_at"],
        invalidated_by=row["invalidated_by"],
        reason=row["reason"],
        source_event_id=row["source_event_id"],
    )
