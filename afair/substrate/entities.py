"""Entity-graph substrate primitives (Phase 4 Track 1).

This module owns the *table-level* surface for the five entity-graph
tables defined in ``schema.py``. The canonicalization *logic* (deciding
which surface form maps to which canonical entity) lives in
``agents/entity_canonicalizer.py``; this module only persists the
decisions.

Identity convention
-------------------
Entity IDs are content-derived:

    entity:<sha256(lowercase(canonical_name)|kind)>

so a rebuild of the entity graph from substrate is deterministic — the
same canonical decisions yield the same IDs. Mention / edge / merge /
invalidation rows use ULIDs for stable time-ordered IDs.

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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from ulid import ULID

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable


# ── identity ──────────────────────────────────────────────────────────────


def entity_id(canonical_name: str, kind: str) -> str:
    """Compute the content-derived ID for a (canonical_name, kind) pair.

    Normalization: case-insensitive on the name, exact match on kind. The
    same human reference written ``Sajinth`` and ``sajinth`` both produce
    the same ID; ``Sajinth`` as person vs project produces two.
    """
    payload = f"{canonical_name.strip().lower()}|{kind}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"entity:{digest}"


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


# ── writes ────────────────────────────────────────────────────────────────


def write_entity(
    conn: sqlite3.Connection,
    *,
    canonical_name: str,
    kind: str,
    created_by: str,
    source_event_id: str,
    confidence: float,
) -> Entity:
    """Insert (or return existing) a canonical entity row.

    Idempotent on the derived entity_id. Two calls with the same
    (canonical_name, kind) return the original row — neither the
    confidence nor source_event_id is updated, by design: per I2, the
    first creation context is preserved.
    """
    eid = entity_id(canonical_name, kind)
    existing = read_entity_by_id(conn, eid)
    if existing is not None:
        return existing

    created_at = _now_iso()
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
    except Exception as exc:
        # IntegrityError on the UNIQUE constraint = already linked.
        # Surface anything else.
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
    except Exception as exc:
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
    except Exception as exc:
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


# ── reads ─────────────────────────────────────────────────────────────────


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
        row = conn.execute(
            """
            SELECT into_entity_id FROM entity_merges
            WHERE from_entity_id = ?
            ORDER BY merged_at DESC LIMIT 1
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


def read_entities_batch(conn: sqlite3.Connection, entity_ids: Iterable[str]) -> dict[str, Entity]:
    """Bulk-fetch entities by ID. Used by recall to materialize the
    canonical_entities surface on many hits in one query.

    Accepts any iterable (list, set, dict values view); deduplicates
    internally — callers don't need to ``list(set(...))`` first.
    """
    unique_ids = list({e for e in entity_ids if e})
    if not unique_ids:
        return {}
    placeholders = ",".join("?" for _ in unique_ids)
    rows = conn.execute(
        f"SELECT * FROM entities WHERE id IN ({placeholders})",
        unique_ids,
    ).fetchall()
    return {row["id"]: _row_to_entity(row) for row in rows}


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

    # Build the VALUES clause for the seed; each id becomes one row.
    seed_values = ",".join("(?)" for _ in unique)
    rows = conn.execute(
        f"""
        WITH RECURSIVE
        latest_merges(from_entity_id, into_entity_id) AS (
            -- For each from_entity_id keep only the most recent merge row.
            -- Matches the per-id helper's ORDER BY merged_at DESC LIMIT 1.
            SELECT from_entity_id, into_entity_id FROM (
                SELECT
                    from_entity_id,
                    into_entity_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY from_entity_id
                        ORDER BY merged_at DESC
                    ) AS rn
                FROM entity_merges
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
        unique,
    ).fetchall()

    # rows contains one entry per start_id whose current_id is the
    # canonical (deepest-reached) ID. IDs with no merges resolve to
    # themselves via the seed row at depth 0.
    return {row["start_id"]: row["current_id"] for row in rows}


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
