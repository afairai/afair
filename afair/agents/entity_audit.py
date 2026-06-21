"""Entity-audit worker — proposes corrections for the operator to confirm.

This is the proactive side of the belief-correction model (ADR-0002): instead
of waiting for the operator to notice that "Maxime" was filed as a person or
that "Bräuer" and "Dr. Gregor Bräuer" are the same person, a cold-path worker
scans the graph and writes *proposals* into ``proposed_corrections``. It never
applies anything — a proposal is a suggestion the operator confirms (or an
LLM-free surface shows them). The applied correction (retype / merge) is the
append-only part; this worker only detects.

Detection is deliberately deterministic and high-precision for v0 — the same
patterns that produced the real errors we fixed by hand:

- a ``person`` whose name is a domain (``maxime.team``) → propose retype to
  ``product``;
- a ``person`` whose name carries a citation year (``Menon 2011``) → propose
  retype to ``concept``;
- two same-kind entities where one name is a shorter form of the other
  (``Bräuer`` ⊂ ``Dr. Gregor Bräuer``) → propose a merge.

An LLM judge for subtler type mismatches is a later addition — and it must
carry the same evidence discipline as the extractor (quote the mention that
justifies the call), or the detector confabulates the very thing it audits.
"""

from __future__ import annotations

import json
import re
from itertools import combinations
from typing import TYPE_CHECKING, Any

import structlog
from ulid import ULID

from .cold_path import ColdPathWorker

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

    from ..settings import Settings

log = structlog.get_logger(__name__)

AUDIT_PRODUCED_BY = "entity_audit:v0"

# A name ending in a known TLD reads as a domain/site, i.e. a product, not a
# person. Kept to common TLDs to stay high-precision (a person surname won't
# match "<word>.<tld>").
_DOMAIN_RE = re.compile(
    r"\b[\w-]+\.(team|me|com|ai|io|app|dev|net|org|co|xyz|de|ch|eu)\b",
    re.IGNORECASE,
)
# A 4-digit 19xx/20xx year in a name reads as a citation/reference.
_CITATION_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def detect_type_mismatch(canonical_name: str, kind: str) -> tuple[str, str, float] | None:
    """If the name pattern contradicts the entity kind, return
    ``(suggested_kind, evidence, confidence)``; else None.

    Only audits ``person`` for v0 — that's where the costly errors landed
    (a tool / a citation filed as someone you know).
    """
    if kind != "person":
        return None
    if _DOMAIN_RE.search(canonical_name):
        return (
            "product",
            f"the name '{canonical_name}' is a domain (has a TLD) — a product/site, not a person",
            0.9,
        )
    if _CITATION_YEAR_RE.search(canonical_name):
        return (
            "concept",
            f"the name '{canonical_name}' contains a year — reads as a citation/reference, not a person",
            0.8,
        )
    return None


def _name_tokens(name: str) -> frozenset[str]:
    return frozenset(t.lower() for t in _TOKEN_RE.findall(name))


def find_merge_candidates(
    entities: list[tuple[str, str, str]],
) -> list[tuple[str, str, str, float]]:
    """Surface-form duplicates: same kind, and one name's tokens are a strict
    subset of the other's (so the shorter is a short form of the longer).

    ``entities`` is ``(entity_id, canonical_name, kind)``. Returns
    ``(from_entity_id, into_entity_id, evidence, confidence)`` — always merging
    the shorter form INTO the fuller name. A heuristic, hence a *proposal*:
    "Marc" ⊂ "Marc Andreessen" might be two different Marcs, so the operator
    confirms.
    """
    out: list[tuple[str, str, str, float]] = []
    by_kind: dict[str, list[tuple[str, str, frozenset[str]]]] = {}
    for eid, name, kind in entities:
        by_kind.setdefault(kind, []).append((eid, name, _name_tokens(name)))
    for group in by_kind.values():
        for (id_a, name_a, ta), (id_b, name_b, tb) in combinations(group, 2):
            if not ta or not tb or ta == tb:
                continue
            if ta < tb:
                out.append((id_a, id_b, f"'{name_a}' is a shorter form of '{name_b}'", 0.85))
            elif tb < ta:
                out.append((id_b, id_a, f"'{name_b}' is a shorter form of '{name_a}'", 0.85))
    return out


def _insert_proposal(
    conn: sqlite3.Connection,
    *,
    kind: str,
    entity_id: str,
    detail: dict[str, Any],
    evidence: str,
    confidence: float,
    now: datetime,
) -> bool:
    """INSERT OR IGNORE a proposal. Returns True if a new row landed (the
    UNIQUE(kind, entity_id) means an already-proposed-or-decided one is left
    untouched — the audit never re-opens a closed proposal)."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO proposed_corrections (
            id, kind, entity_id, detail, evidence, confidence, tier,
            detected_by, detected_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, 'review', ?, ?, 'proposed')
        """,
        (
            str(ULID()),
            kind,
            entity_id,
            json.dumps(detail, sort_keys=True),
            evidence,
            confidence,
            AUDIT_PRODUCED_BY,
            now.isoformat(),
        ),
    )
    return cur.rowcount > 0


class EntityAuditWorker(ColdPathWorker):
    """Scan the entity graph and queue corrections for the operator to confirm.
    Detection only — never applies. No LLM in v0 (deterministic heuristics)."""

    name = "entity_audit"
    interval_seconds = 12 * 3600  # twice a day — the graph changes slowly

    def run(self, conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        # Current entities only (not merged away).
        rows = conn.execute(
            """
            SELECT e.id, e.canonical_name, e.kind
            FROM entities e
            LEFT JOIN entity_merges m ON m.from_entity_id = e.id
            WHERE m.id IS NULL
            """,
        ).fetchall()
        entities = [(r["id"], r["canonical_name"], r["kind"]) for r in rows]

        stats: dict[str, Any] = {
            "scanned": len(entities),
            "retype_proposals": 0,
            "merge_proposals": 0,
        }
        with conn:
            for eid, name, kind in entities:
                tm = detect_type_mismatch(name, kind)
                if tm is not None:
                    to_kind, evidence, conf = tm
                    if _insert_proposal(
                        conn,
                        kind="retype",
                        entity_id=eid,
                        detail={"from_kind": kind, "to_kind": to_kind, "name": name},
                        evidence=evidence,
                        confidence=conf,
                        now=now,
                    ):
                        stats["retype_proposals"] += 1
            for from_id, into_id, evidence, conf in find_merge_candidates(entities):
                if _insert_proposal(
                    conn,
                    kind="merge",
                    entity_id=from_id,
                    detail={"into_entity_id": into_id},
                    evidence=evidence,
                    confidence=conf,
                    now=now,
                ):
                    stats["merge_proposals"] += 1

        if stats["retype_proposals"] or stats["merge_proposals"]:
            log.info("entity_audit.proposals", **stats)
        return stats
