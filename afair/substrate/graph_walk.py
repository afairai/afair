"""Bounded multi-hop expansion over the entity graph (read-only).

The entity graph has always been built and served, but never *searched*: it
rides along as a per-hit overlay on events that some other retrieval arm had
already found. A question whose answer sits one relation away from the words
the user typed ("who else works on the thing Michael builds with the colleague
from vkb") therefore came back empty, because no arm of recall ever followed an
edge.

This module closes that gap the way Graphiti (getzep, Apache-2.0) does
conceptually — seed nodes, then a breadth-first walk over relations, then the
source documents of what the walk reached. Nothing is copied: Graphiti walks a
Neo4j/FalkorDB property graph with an LLM-built ontology, this walks the
existing SQLite ``entity_edges`` tables and honors afair's own belief layer
(invalidated edges, retracted entities, ADR-0004 served confidence).

Three properties are load-bearing and enforced here rather than left to the
caller:

* **Bounded.** Every axis has a hard ceiling (:class:`WalkLimits`): hops,
  seeds, fanout per entity, total entities, events per entity, total events.
  A walk cannot fan out into the whole vault however densely connected it is.
* **Hub-suppressed.** An entity above ``hub_degree_max`` (the operator's own
  name, "Jarvis", "Notion") is reachable but never *expanded through* — hubs
  connect everything to everything, so traversing one adds noise, not answers.
  Same instinct as the hub suppression in ``agents/living_syntheses.py``.
* **Deterministic.** Every ordering has explicit tiebreakers down to the row
  id. The same vault and the same seeds always produce the same walk, which is
  what makes it testable and what keeps recall reproducible.
* **Current.** Only relations that hold now are traversed. Both of ADR-0008's
  clocks are honored: world time through the ``edge_validity_spans`` sidecar (a
  fact that stopped being true, or has not started yet, is not a road) and
  knowledge time through ``edge_invalidations`` (a belief afair has retired).
  Neither hides anything — the events remain findable through every other
  recall arm; they are simply not treated as live structure.

Read-only: this module issues SELECTs and nothing else. It writes no table,
schedules no worker, and calls no model.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .edge_confidence import latest_edge_confidence_batch
from .edge_validity import latest_edge_validity_batch, normalize_temporal_bound
from .entities import resolve_canonical_batch, retracted_entity_ids
from .events import row_to_event
from .sqlutil import iter_param_chunks

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

    from .events import Event


@dataclass(frozen=True)
class WalkLimits:
    """Every ceiling the walk obeys. Frozen so a caller cannot widen one in place.

    The defaults are chosen so a worst-case walk touches a few hundred rows
    across a handful of indexed queries — the cost of one extra recall arm, not
    the cost of a graph query engine.
    """

    max_hops: int = 2
    """Never more than two. One hop answers "what is directly related", two
    answers "what is related to that". Three is where a personal vault turns
    into everything-connects-to-everything."""

    max_seed_events: int = 5
    """How many of the already-ranked hits contribute their entities as seeds."""

    max_seeds: int = 8
    """Cap on distinct seed entities, after canonical resolution."""

    max_fanout_per_entity: int = 8
    """Edges followed out of any single entity, best-confidence first."""

    max_entities_total: int = 40
    """Cap on distinct entities discovered across all hops, seeds included."""

    max_events_per_entity: int = 5
    """Newest-first cap per discovered entity, so one busy entity cannot fill
    the whole result."""

    max_events_total: int = 50
    """Cap on events returned by the walk."""

    hub_degree_max: int = 60
    """An entity with more edges than this is never expanded through."""

    min_edge_confidence: float = 0.35
    """Edges below this served confidence (ADR-0004) are not followed. Low
    confidence edges stay *servable* with a caveat elsewhere in recall; this is
    only about whether one is load-bearing enough to walk across."""


DEFAULT_LIMITS = WalkLimits()


@dataclass(frozen=True)
class WalkStats:
    """What the walk actually did — for the caller's note and for tests."""

    seeds: int
    entities_discovered: int
    hops_used: int
    hubs_skipped: int
    events_found: int
    truncated: bool
    """True when any ceiling actually bit, i.e. the walk stopped early."""

    expired_skipped: int = 0
    """Edges whose world-time validity had already ended (ADR-0008)."""

    not_yet_valid_skipped: int = 0
    """Edges whose world-time validity has not started yet."""


@dataclass(frozen=True)
class _EdgeRow:
    """One live edge as the walk reads it, before the validity overlay."""

    edge_id: str
    other_id: str
    confidence: float
    discovered_at: str
    column_valid_from: str | None
    column_valid_to: str | None


def _parse_bound(value: str | None) -> datetime | None:
    """Parse a stored bound defensively; malformed legacy data reads as unknown.

    Mirrors ``edge_validity._safe_existing_bound``: a date this module cannot
    understand must not silently exclude a fact from the graph, so it degrades
    to "no bound known" rather than to "not valid".
    """
    if value is None:
        return None
    try:
        normalized = normalize_temporal_bound(value)
    except ValueError:
        return None
    if normalized is None:
        return None
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:  # pragma: no cover — normalize_temporal_bound guarantees this parses
        return None


def is_currently_valid(
    valid_from: str | None, valid_to: str | None, now: datetime
) -> tuple[bool, str | None]:
    """Whether ``[valid_from, valid_to)`` contains ``now``.

    Returns ``(valid, reason)`` where reason is ``"expired"``, ``"not_yet"`` or
    ``None`` — the caller wants to count the two rejection kinds separately, and
    a bare bool would throw that away.

    Half-open interval, matching ``edge_validity.edge_is_valid_at``: a fact
    whose ``valid_to`` is exactly now has already stopped being true.
    """
    start = _parse_bound(valid_from)
    if start is not None and now < start:
        return False, "not_yet"
    end = _parse_bound(valid_to)
    if end is not None and now >= end:
        return False, "expired"
    return True, None


def effective_validity_batch(
    conn: sqlite3.Connection,
    column_bounds: dict[str, tuple[str | None, str | None]],
) -> dict[str, tuple[str | None, str | None]]:
    """Effective ``(valid_from, valid_to)`` per edge, sidecar first.

    The composition rule is ADR-0008's: ``edge_validity_spans`` holds later
    corrections and its latest row wins outright (including a deliberate
    narrowing to a single open bound), while the immutable ``entity_edges``
    columns remain the at-discovery snapshot used when no span exists yet —
    legacy edges, or edges the backfill has not reached.

    Batched through ``latest_edge_validity_batch`` and chunked, so a wide walk
    costs a bounded number of queries rather than one per edge.
    """
    if not column_bounds:
        return {}
    out: dict[str, tuple[str | None, str | None]] = dict(column_bounds)
    edge_ids = sorted(column_bounds)
    for chunk in iter_param_chunks(edge_ids):
        for edge_id, span in latest_edge_validity_batch(conn, chunk).items():
            out[edge_id] = (span.valid_from, span.valid_to)
    return out


@dataclass(frozen=True)
class _Reached:
    """One entity the walk reached, with how it got there."""

    entity_id: str
    hop: int
    confidence: float
    """Confidence of the edge that reached it; 1.0 for a seed."""


def _fetch_degrees(conn: sqlite3.Connection, entity_ids: Sequence[str]) -> dict[str, int]:
    """Edge degree (subject side + object side) per entity, in one pass.

    Invalidated edges are excluded so a retired relation cannot push an entity
    over the hub threshold and silently make it unwalkable.
    """
    degrees: dict[str, int] = dict.fromkeys(entity_ids, 0)
    if not entity_ids:
        return degrees
    for chunk in iter_param_chunks(list(entity_ids)):
        placeholders = ",".join("?" * len(chunk))
        # UNION ALL of two independently-indexed arms rather than one OR: SQLite
        # only applies its OR-optimization within a single index, so the OR form
        # degrades to a scan of entity_edges (the same reason the entity-match
        # lookup in the recall handler is written as a UNION).
        rows = conn.execute(
            f"""
            SELECT entity_id, COUNT(*) AS degree FROM (
                SELECT e.subject_id AS entity_id FROM entity_edges e
                LEFT JOIN edge_invalidations i ON i.edge_id = e.id
                WHERE e.subject_id IN ({placeholders}) AND i.id IS NULL
                UNION ALL
                SELECT e.object_id AS entity_id FROM entity_edges e
                LEFT JOIN edge_invalidations i ON i.edge_id = e.id
                WHERE e.object_id IN ({placeholders}) AND i.id IS NULL
            )
            GROUP BY entity_id
            """,
            [*chunk, *chunk],
        ).fetchall()
        for row in rows:
            degrees[row["entity_id"]] = row["degree"]
    return degrees


def _fetch_edges(conn: sqlite3.Connection, entity_ids: Sequence[str]) -> dict[str, list[_EdgeRow]]:
    """Live edges touching each entity, keyed by the anchor entity.

    Confidence and validity are the at-discovery columns here; the caller
    overlays the ADR-0004 served score and the ADR-0008 validity sidecar.
    Invalidated edges — knowledge-time expiry — are filtered in SQL already,
    matching the default of ``entities.iter_edges_for_entity``.
    """
    out: dict[str, list[_EdgeRow]] = {eid: [] for eid in entity_ids}
    if not entity_ids:
        return out
    for chunk in iter_param_chunks(list(entity_ids)):
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""
            SELECT e.subject_id AS anchor, e.object_id AS other,
                   e.id AS edge_id, e.confidence AS confidence,
                   e.discovered_at AS discovered_at,
                   e.valid_from AS valid_from, e.valid_to AS valid_to
            FROM entity_edges e
            LEFT JOIN edge_invalidations i ON i.edge_id = e.id
            WHERE e.subject_id IN ({placeholders}) AND i.id IS NULL
            UNION ALL
            SELECT e.object_id AS anchor, e.subject_id AS other,
                   e.id AS edge_id, e.confidence AS confidence,
                   e.discovered_at AS discovered_at,
                   e.valid_from AS valid_from, e.valid_to AS valid_to
            FROM entity_edges e
            LEFT JOIN edge_invalidations i ON i.edge_id = e.id
            WHERE e.object_id IN ({placeholders}) AND i.id IS NULL
            """,
            [*chunk, *chunk],
        ).fetchall()
        for row in rows:
            out.setdefault(row["anchor"], []).append(
                _EdgeRow(
                    edge_id=row["edge_id"],
                    other_id=row["other"],
                    confidence=float(row["confidence"]),
                    discovered_at=row["discovered_at"],
                    column_valid_from=row["valid_from"],
                    column_valid_to=row["valid_to"],
                )
            )
    return out


def seed_entities(
    conn: sqlite3.Connection,
    events: Sequence[Event],
    *,
    limits: WalkLimits = DEFAULT_LIMITS,
) -> list[str]:
    """Canonical entity ids to start the walk from, taken from the top hits.

    Seeding from the events recall already ranked (rather than from a literal
    name match on the query string) is what makes the walk work for prose
    questions: "what is blocking the memory work" seeds from whatever those
    words retrieved, and the walk carries on from there.

    Order follows the incoming event ranking, then mention order within an
    event. Deduped after merge resolution, so two surface forms of one entity
    spend one seed slot, not two.
    """
    head = list(events[: limits.max_seed_events])
    if not head:
        return []

    hashes = [e.content_hash for e in head]
    rank = {content_hash: i for i, content_hash in enumerate(hashes)}
    mentions: list[tuple[int, str, str]] = []
    for chunk in iter_param_chunks(hashes):
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""
            SELECT event_hash, entity_id, id FROM entity_mentions
            WHERE event_hash IN ({placeholders})
            """,
            chunk,
        ).fetchall()
        mentions.extend(
            (rank[row["event_hash"]], row["id"], row["entity_id"])
            for row in rows
            if row["event_hash"] in rank
        )
    if not mentions:
        return []
    # Event ranking first, mention order (ULID = write order) within an event.
    mentions.sort()
    raw_ids = [entity_id for _rank, _mid, entity_id in mentions]

    resolved = resolve_canonical_batch(conn, list(dict.fromkeys(raw_ids)))
    retracted = retracted_entity_ids(conn)
    seeds: list[str] = []
    seen: set[str] = set()
    for raw in raw_ids:
        canonical = resolved.get(raw, raw)
        if canonical in seen or canonical in retracted:
            continue
        seen.add(canonical)
        seeds.append(canonical)
        if len(seeds) >= limits.max_seeds:
            break
    return seeds


def _walk(
    conn: sqlite3.Connection,
    seeds: Sequence[str],
    *,
    limits: WalkLimits,
    now: datetime,
) -> tuple[list[_Reached], int, bool, int, int]:
    """Breadth-first expansion.

    Returns ``(reached, hubs_skipped, truncated, expired_skipped,
    not_yet_valid_skipped)``.
    """
    retracted = retracted_entity_ids(conn)
    reached: list[_Reached] = [
        _Reached(entity_id=s, hop=0, confidence=1.0) for s in seeds if s not in retracted
    ]
    visited: set[str] = {r.entity_id for r in reached}
    frontier: list[_Reached] = list(reached)
    hubs_skipped = 0
    truncated = False
    expired_skipped = 0
    not_yet_valid_skipped = 0

    for hop in range(1, limits.max_hops + 1):
        if not frontier or len(visited) >= limits.max_entities_total:
            break
        frontier_ids = [r.entity_id for r in frontier]
        degrees = _fetch_degrees(conn, frontier_ids)
        expandable = [eid for eid in frontier_ids if degrees.get(eid, 0) <= limits.hub_degree_max]
        hubs_skipped += len(frontier_ids) - len(expandable)
        if not expandable:
            truncated = truncated or bool(frontier_ids)
            break

        edges_by_entity = _fetch_edges(conn, expandable)
        all_edges = [row for edges in edges_by_entity.values() for row in edges]
        all_edge_ids = [row.edge_id for row in all_edges]
        # ADR-0004: the *served* confidence is the latest score row; the column
        # is only the at-discovery snapshot. Walking on the frozen column would
        # follow edges the confidence model has since talked itself out of.
        served = latest_edge_confidence_batch(conn, all_edge_ids)
        # ADR-0008: same argument on the other clock. The immutable columns are
        # the at-discovery snapshot; a later correction lives in the validity
        # sidecar. One batched read for the whole hop, never one per edge.
        validity = effective_validity_batch(
            conn,
            {row.edge_id: (row.column_valid_from, row.column_valid_to) for row in all_edges},
        )

        next_frontier: list[_Reached] = []
        for eid in expandable:
            candidates = [
                (
                    row.edge_id,
                    row.other_id,
                    served.get(row.edge_id, row.confidence),
                    row.discovered_at,
                )
                for row in edges_by_entity.get(eid, [])
            ]
            # Best confidence first; then newest; then edge id. The last two
            # exist purely to make the truncation at max_fanout_per_entity
            # reproducible rather than dependent on row order. Built as three
            # stable sorts from weakest to strongest key, which keeps each
            # field's direction independent (a string cannot be negated inside
            # a single composite key).
            candidates.sort(key=lambda c: c[0])  # edge id, ascending
            candidates.sort(key=lambda c: c[3], reverse=True)  # discovered_at, newest
            candidates.sort(key=lambda c: c[2], reverse=True)  # served confidence
            followed = 0
            for edge_id, other, confidence, _discovered in candidates:
                if followed >= limits.max_fanout_per_entity:
                    truncated = True
                    break
                if confidence < limits.min_edge_confidence:
                    continue
                # World-time gate. A fact that has stopped being true, or has
                # not started yet, is not a current relation — it stays fully
                # readable as history, it just is not a road the walk drives
                # down when asked what holds NOW. Rejected BEFORE the fanout
                # counter so a cluster of expired edges cannot consume an
                # entity's budget and starve its live ones.
                valid_from, valid_to = validity.get(edge_id, (None, None))
                is_valid, why_not = is_currently_valid(valid_from, valid_to, now)
                if not is_valid:
                    if why_not == "expired":
                        expired_skipped += 1
                    else:
                        not_yet_valid_skipped += 1
                    continue
                followed += 1
                if other in visited or other in retracted:
                    continue
                if len(visited) >= limits.max_entities_total:
                    truncated = True
                    break
                visited.add(other)
                node = _Reached(entity_id=other, hop=hop, confidence=confidence)
                reached.append(node)
                next_frontier.append(node)
            if len(visited) >= limits.max_entities_total:
                truncated = True
                break
        frontier = next_frontier

    return reached, hubs_skipped, truncated, expired_skipped, not_yet_valid_skipped


def _events_for_entities(
    conn: sqlite3.Connection,
    reached: Sequence[_Reached],
    *,
    limits: WalkLimits,
    exclude_hashes: set[str],
) -> list[Event]:
    """Source events of the reached entities, newest-first, capped per entity.

    Mentions are stored against the *raw* entity id that was current when the
    mention was written, so a later merge would hide the older surface. We look
    up both the canonical ids and every id that merges into them; that covers
    the merge case without reimplementing the full merge closure here. Entities
    reachable only through a chain of several merges may still be missed — a
    known and accepted limit of this read path, not a silent one.
    """
    if not reached:
        return []
    canonical_ids = [r.entity_id for r in reached]
    lookup_ids = set(canonical_ids)
    for chunk in iter_param_chunks(canonical_ids):
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""
            SELECT m.from_entity_id AS alias FROM entity_merges m
            LEFT JOIN merge_invalidations mi ON mi.merge_id = m.id
            WHERE m.into_entity_id IN ({placeholders}) AND mi.id IS NULL
            """,
            chunk,
        ).fetchall()
        lookup_ids.update(row["alias"] for row in rows)

    alias_to_canonical: dict[str, str] = {cid: cid for cid in canonical_ids}
    resolved = resolve_canonical_batch(conn, sorted(lookup_ids))
    for raw, canonical in resolved.items():
        alias_to_canonical.setdefault(raw, canonical)

    # hop, then reaching-edge confidence, then id — the per-entity priority when
    # the total event budget runs out.
    priority = {
        r.entity_id: (r.hop, -r.confidence, r.entity_id)
        for r in sorted(reached, key=lambda r: (r.hop, -r.confidence, r.entity_id))
    }

    per_entity: dict[str, list[Event]] = {}
    for chunk in iter_param_chunks(sorted(lookup_ids)):
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""
            SELECT DISTINCT m.entity_id AS matched_entity, ev.*
            FROM entity_mentions m
            JOIN events ev ON ev.id = m.event_id
            WHERE m.entity_id IN ({placeholders})
            ORDER BY ev.created_at DESC, ev.id DESC
            """,
            chunk,
        ).fetchall()
        for row in rows:
            owner = alias_to_canonical.get(row["matched_entity"])
            if owner is None or owner not in priority:
                continue
            bucket = per_entity.setdefault(owner, [])
            if len(bucket) >= limits.max_events_per_entity:
                continue
            event = row_to_event(row)
            if event.content_hash in exclude_hashes:
                continue
            bucket.append(event)

    out: list[Event] = []
    seen_hashes: set[str] = set()
    for entity_id in sorted(priority, key=priority.__getitem__):
        for event in per_entity.get(entity_id, []):
            if event.content_hash in seen_hashes:
                continue
            seen_hashes.add(event.content_hash)
            out.append(event)
            if len(out) >= limits.max_events_total:
                return out
    return out


def walk_from_events(
    conn: sqlite3.Connection,
    events: Sequence[Event],
    *,
    limits: WalkLimits = DEFAULT_LIMITS,
    now: str | datetime | None = None,
) -> tuple[list[Event], WalkStats]:
    """Expand from already-ranked hits into events one or two relations away.

    Only relations that hold **now** are traversed: an edge whose world-time
    validity has ended, or has not begun, is not a current road through the
    graph (ADR-0008). Knowledge-time expiry — an edge invalidation — remains a
    separate and equally hard boundary, applied in SQL. Neither deletes
    anything: the events stay findable through every other recall arm, and the
    history lens is a later, explicit feature.

    ``now`` is injectable so tests can pin the clock; production leaves it at
    the current UTC time.

    Returns the newly-reached events (never anything already in ``events``) plus
    the walk's statistics. An empty result is the normal outcome for a vault
    whose graph has nothing to add, and the caller should treat it as a no-op
    rather than an error.
    """
    moment = _parse_bound(normalize_temporal_bound(now)) if now is not None else None
    if moment is None:
        moment = datetime.now(UTC)

    seeds = seed_entities(conn, events, limits=limits)
    if not seeds:
        return [], WalkStats(
            seeds=0,
            entities_discovered=0,
            hops_used=0,
            hubs_skipped=0,
            events_found=0,
            truncated=False,
        )

    reached, hubs_skipped, truncated, expired, not_yet = _walk(
        conn, seeds, limits=limits, now=moment
    )
    hops_used = max((r.hop for r in reached), default=0)
    exclude = {e.content_hash for e in events}
    found = _events_for_entities(conn, reached, limits=limits, exclude_hashes=exclude)
    stats = WalkStats(
        seeds=len(seeds),
        entities_discovered=len(reached),
        hops_used=hops_used,
        hubs_skipped=hubs_skipped,
        events_found=len(found),
        truncated=truncated or len(found) >= limits.max_events_total,
        expired_skipped=expired,
        not_yet_valid_skipped=not_yet,
    )
    return found, stats
