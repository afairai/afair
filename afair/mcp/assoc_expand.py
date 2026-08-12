"""Chain-Arm — result-level graph expansion for multi-entity queries (P1b).

The existing arms cover two shapes: ``_events_via_entity_match`` fires when
the WHOLE query is one entity reference, and the depth="deep" walk expands
from already-found events over hard edges. Neither covers the multihop shape
the vault-native benchmark measures: a query naming TWO entities ("S O")
whose connection runs through a hidden middle entity M — the evidence events
mention {S,M} and {M,O}, match only half the query text each, and rank below
the fold (baseline: multihop both@8 = 5%).

This arm assembles the chain instead of hoping text rank finds it:

  1. detect entity names INSIDE the query (bounded n-grams against the same
     lowered canonical/surface indexes the entity arm uses),
  2. expand each by one hop: hard edges (entity_edges, both directions) plus
     SOFT associations from the sidecar DB (``<vault>/associations.db`` —
     co-mention/co-day weights built offline; measured +45pp on multihop
     any@8 before this integration, see benchmark results 2026-08-11),
  3. rank events by how many DISTINCT expanded entities they mention.

Only events matching >= 2 expanded entities are returned: one shared entity
is what plain text search already finds, two is the chain signal. This floor
is also the abstention guard — for two genuinely unrelated query entities no
event clears it, so the arm adds nothing rather than fabricating a link.

Soft associations assert NO fact ("these concepts recur together in the
operator's life", not "S relates to O") — they only navigate retrieval.
Hard-fact precision therefore stays untouched by this arm; it reorders
evidence, it never invents edges.

Fails soft everywhere: missing sidecar, malformed rows, or any SQL error
degrade to "no chain candidates" and recall behaves exactly as before.
Kill switch: ``AFAIR_CHAIN_RECALL=0``.
"""

from __future__ import annotations

import os
import sqlite3
from typing import TYPE_CHECKING

import structlog

from ..substrate.events import Event, row_to_event

if TYPE_CHECKING:
    from pathlib import Path

log = structlog.get_logger(__name__)

MAX_QUERY_TOKENS = 12
"""Longer queries are prose questions; n-gram entity spotting on them gets
quadratic and matches noise. The benchmark/multihop shape is short."""

MAX_NGRAM = 3
"""Entity names in this vault are 1-3 tokens (canonical names are compact)."""

MAX_QUERY_ENTITIES = 4
"""Expansion is per-entity; more than a handful means the query is a list,
not a relation question."""

NEIGHBORS_PER_ENTITY = 6
"""One-hop fan-out cap per query entity, hard and soft arms each."""

BRIDGE_NEIGHBORS_PER_ENTITY = 25
"""Wider cap used ONLY for computing the bridge intersection. Measured
2026-08-12: with the tight cap the true middle entity of a hub-adjacent
chain (Jarvis—Mac Mini—Tailnet) fell out of the top-6 soft neighbors and
the bridge set degenerated to generic hubs. Widening the CANDIDATE pool
for the intersection is cheap; the intersection itself stays small."""

MAX_CHAIN_EVENTS = 16
"""Arm output cap — the RRF merge downstream only rewards the head anyway."""

_ENV_KILL = "AFAIR_CHAIN_RECALL"


def _desc(iso: str) -> str:
    """Sort helper: maps an ISO timestamp to a string whose ASCENDING order
    is the timestamp's DESCENDING order (char-wise 9's complement)."""
    return "".join(chr(255 - ord(c)) for c in iso)


def _enabled() -> bool:
    return os.environ.get(_ENV_KILL, "1").strip() != "0"


def _query_entity_names(db: sqlite3.Connection, query: str) -> list[str]:
    """Canonical entity names appearing verbatim (case-insensitive) in the
    query, longest n-gram first so "Mac Mini" wins over "Mini"."""
    tokens = [t for t in query.split() if t]
    if not tokens or len(tokens) > MAX_QUERY_TOKENS:
        return []
    grams: list[str] = []
    for n in range(min(MAX_NGRAM, len(tokens)), 0, -1):
        grams += [" ".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1)]
    found: list[str] = []
    claimed: set[str] = set()
    for g in grams:
        gl = g.lower()
        if gl in claimed or len(gl) < 3:
            continue
        row = db.execute(
            """
            SELECT canonical_name FROM entities
            WHERE LOWER(canonical_name) = :g
            UNION
            SELECT ent.canonical_name FROM entity_mentions em
            JOIN entities ent ON ent.id = em.entity_id
            WHERE LOWER(em.surface_form) = :g
            LIMIT 1
            """,
            {"g": gl},
        ).fetchone()
        if row:
            name = row["canonical_name"]
            if name not in found:
                found.append(name)
                # claim the covering tokens so sub-grams don't double-match
                claimed.update(gl.split())
                claimed.add(gl)
        if len(found) >= MAX_QUERY_ENTITIES:
            break
    return found


def _hard_neighbors(
    db: sqlite3.Connection, names: list[str], limit: int = NEIGHBORS_PER_ENTITY
) -> set[str]:
    out: set[str] = set()
    for n in names:
        rows = db.execute(
            """
            SELECT eo.canonical_name x FROM entity_edges e
            JOIN entities es ON es.id = e.subject_id
            JOIN entities eo ON eo.id = e.object_id
            WHERE LOWER(es.canonical_name) = LOWER(?) AND e.valid_to IS NULL
            UNION
            SELECT es2.canonical_name FROM entity_edges e2
            JOIN entities es2 ON es2.id = e2.subject_id
            JOIN entities eo2 ON eo2.id = e2.object_id
            WHERE LOWER(eo2.canonical_name) = LOWER(?) AND e2.valid_to IS NULL
            LIMIT ?
            """,
            (n, n, limit),
        ).fetchall()
        # UNION inherits the first arm's column alias — every row carries "x".
        out.update(r["x"] for r in rows)
    return out


def _soft_neighbors(
    vault_dir: Path, names: list[str], limit: int = NEIGHBORS_PER_ENTITY
) -> set[str]:
    """Top associated names from the offline-built sidecar; absent file or
    schema mismatch → empty set (the sidecar is an enrichment, not a dep)."""
    path = vault_dir / "associations.db"
    if not path.exists():
        return set()
    out: set[str] = set()
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            for n in names:
                for (other,) in con.execute(
                    "SELECT other FROM assoc_weights WHERE name = ? "
                    "ORDER BY weight DESC LIMIT ?",
                    (n, limit),
                ):
                    out.add(other)
        finally:
            con.close()
    except sqlite3.Error as exc:
        log.warning("chain_recall.sidecar_failed", error=str(exc))
        return set()
    return out


def events_via_chain(
    db: sqlite3.Connection, vault_dir: Path, query: str, *, limit: int = MAX_CHAIN_EVENTS
) -> list[Event]:
    """Evidence events connecting the query's entities, best chain first.

    First cut ranked "any event mentioning >=2 of the expanded soup" — and
    drowned in the hub problem: in a lived-in vault the popular entities
    (the operator, the machine, the main project) co-occur in DOZENS of
    session summaries, so recent generic events buried the actual evidence
    (measured: multihop both@8 fell to 0%). v2 targets evidence instead:

      Tier 1 (score 3/2): SOURCE events of valid edges incident to a query
        entity — the events the graph itself cites as evidence for a
        relation. Other endpoint on a BRIDGE scores higher.
      Tier 2 (score 1):   events mentioning >=1 query entity AND >=1 bridge,
        where a bridge is an entity in the neighborhood of TWO different
        query entities — the hidden middle of an S—M—O chain. The bridge
        set is small and query-specific, so hubs stop flooding the arm.

    [] on any failure, for single-concept/prose queries, and for genuinely
    unrelated entity pairs (no bridges, no incident edges) — the
    abstention guard."""
    if not _enabled():
        return []
    try:
        names = _query_entity_names(db, query)
        if not names:
            return []
        nbrs: dict[str, set[str]] = {}
        for n in names:
            nbrs[n] = _hard_neighbors(db, [n], BRIDGE_NEIGHBORS_PER_ENTITY) | _soft_neighbors(
                vault_dir, [n], BRIDGE_NEIGHBORS_PER_ENTITY
            )
        bridges: set[str] = set()
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                bridges |= (nbrs[a] & nbrs[b]) - set(names)

        # event_id -> (score, pair_rarity, created_at); rarity: how many source
        # events carry the SAME (s,o) relation name-pair. A 13x-repeated
        # "Jarvis->afair" is generic co-occurrence; a 1x "Mac Mini->Tailnet"
        # is specific evidence. Rarer wins inside a score tier.
        scored: dict[str, tuple[int, int, str]] = {}

        # Tier 1: edge-evidence events incident to a query entity.
        q_ph = ",".join("?" for _ in names)
        tier1_rows = db.execute(
            f"""
            SELECT e.source_event_id sid, es.canonical_name s, eo.canonical_name o,
                   ev.created_at ca
            FROM entity_edges e
            JOIN entities es ON es.id = e.subject_id
            JOIN entities eo ON eo.id = e.object_id
            JOIN events ev ON ev.id = e.source_event_id
            WHERE e.valid_to IS NULL
              AND (es.canonical_name IN ({q_ph}) OR eo.canonical_name IN ({q_ph}))
            """,
            (*names, *names),
        ).fetchall()
        tier1 = list(tier1_rows)
        pair_freq: dict[tuple[str, str], int] = {}
        for r in tier1:
            pair_freq[(r["s"], r["o"])] = pair_freq.get((r["s"], r["o"]), 0) + 1
        # Slot allocation is PER RELATION-PAIR, not per event: a hot pair
        # ("Jarvis—Mac Mini", 4 source events) must not flood the arm with
        # echoes of one relation while the second leg of the chain starves.
        # Within a pair ALL sources ride along, ROOT EVIDENCE (oldest) first
        # — the first assertion established the relation; later ones restate
        # it. Pairs order by score desc, then rarity asc (a 1x-asserted leg
        # is more informative than a 13x hub echo).
        pairs: dict[tuple[str, str], dict] = {}
        for r in tier1:
            other = r["o"] if r["s"] in names else r["s"]
            # Only two edge shapes count as chain evidence: the direct
            # relation between two QUERY entities, and a leg touching a
            # BRIDGE. A hub's unrelated edges (other endpoint neither) were
            # the abstention leak (73% -> 60% measured 2026-08-12): for two
            # genuinely unconnected query entities they fabricated hits.
            if other in bridges:
                score = 3
            elif other in names:
                score = 2  # direct S->O evidence
            else:
                continue
            key = (r["s"], r["o"])
            d = pairs.setdefault(key, {"score": score, "sids": []})
            d["score"] = max(d["score"], score)
            d["sids"].append((r["ca"], r["sid"]))
        # ROUND-ROBIN across pairs: slot 1..k go to the FIRST source of k
        # different relation legs before any leg gets its second source —
        # the downstream page-1 hoist takes the arm's head, and a chain
        # answer needs one event per leg, not three echoes of leg one.
        ordered_pairs = [
            d for _k, d in sorted(
                pairs.items(), key=lambda kv: (-kv[1]["score"], pair_freq[kv[0]])
            )
        ]
        for d in ordered_pairs:
            d["sids"].sort()  # oldest first = root evidence
        rank = 1
        depth_i = 0
        while True:
            hit_any = False
            for d in ordered_pairs:
                if depth_i < len(d["sids"]):
                    ca, sid = d["sids"][depth_i]
                    if sid not in scored:
                        scored[sid] = (d["score"], rank, ca)
                        rank += 1
                    hit_any = True
            if not hit_any:
                break
            depth_i += 1

        # Tier 2: query-entity + bridge co-mentions (chain middles only —
        # NOT the full neighbor soup; that restraint is the hub fix).
        if bridges:
            core = set(names) | bridges
            c_ph = ",".join("?" for _ in core)
            for r in db.execute(
                f"""
                SELECT ev.id sid, ev.created_at ca,
                       SUM(CASE WHEN ent.canonical_name IN ({q_ph}) THEN 1 ELSE 0 END)
                           AS n_query,
                       COUNT(DISTINCT ent.canonical_name) AS n_all
                FROM events ev
                JOIN entity_mentions em ON em.event_id = ev.id
                JOIN entities ent ON ent.id = em.entity_id
                WHERE ent.canonical_name IN ({c_ph})
                GROUP BY ev.id
                HAVING n_query >= 1 AND n_all >= 2
                """,
                (*names, *core),
            ).fetchall():
                if r["sid"] not in scored:
                    scored[r["sid"]] = (1, 9999, r["ca"])

        if not scored:
            return []
        # score desc -> rarer relation-pair first -> newest first
        ids = [
            kv[0]
            for kv in sorted(
                scored.items(),
                key=lambda kv: (-kv[1][0], kv[1][1], _desc(kv[1][2])),
            )
        ][:limit]
        ph = ",".join("?" for _ in ids)
        rows = db.execute(f"SELECT * FROM events WHERE id IN ({ph})", ids).fetchall()
        by_id = {r["id"]: r for r in rows}
        return [row_to_event(by_id[i]) for i in ids if i in by_id]
    except sqlite3.Error as exc:
        log.warning("chain_recall.failed", error=str(exc))
        return []
