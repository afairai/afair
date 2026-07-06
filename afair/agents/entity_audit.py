"""Entity-audit worker — proposes corrections for the operator to confirm.

This is the proactive side of the belief-correction model (ADR-0002): instead
of waiting for the operator to notice that "Maxime" was filed as a person or
that "Bräuer" and "Dr. Gregor Bräuer" are the same person, a cold-path worker
scans the graph and writes *proposals* into ``proposed_corrections``. It never
applies anything — a proposal is a suggestion the operator confirms (or an
LLM-free surface shows them). The applied correction (retype / merge) is the
append-only part; this worker only detects.

Two detectors, both high-precision and chosen from what a dry-run against the
real vault actually showed (the noisy ideas were dropped after they failed it):

- **Cross-kind auto-merge review** (the main signal). The entity-deduplicator
  merges same-name entities split across kinds (``Clario`` project →
  ``Clario`` product, ``VISION.md`` product → ``other``, ...). Those merges
  are automatic and *pick a kind for you* — exactly the "automatic cluster,
  whatever it is, we must check" case. Each such merge becomes a
  ``merge_review`` proposal: confirm the picked kind, or correct it.
- **Deterministic person type-mismatch** (cheap, future-proofing). A
  ``person`` whose name is a domain (``maxime.team`` → ``product``) or carries
  a citation year (``Menon 2011`` → ``concept``) → a ``retype`` proposal. These
  found nothing on the live vault (already fixed by hand), but stay as a cheap
  guard against the same error re-entering.

Dropped after the dry-run: a surface-form subset merger (``"Claude"`` ⊂
``"Claude Code"``) — it proposed mostly *wrong* merges (distinct things), and
fuzzy LLM name-matches (10 on the vault, almost all correct) — low signal. The
right merge signal is the kind the system *already* picked, not a re-derived
guess.
"""

from __future__ import annotations

import json
import re
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

# ── structural-junk detector (I6-safe review suppression) ────────────────────
# Names that are ticket ids, file paths, or config filenames are STRUCTURAL
# tokens the extractor lifted from text — not entities whose KIND anyone needs
# to review. These regexes only SUPPRESS a kind-review proposal; they assign no
# kind, introduce no enum, and never touch the entity or its kind_observations.
_TICKET_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")
"""A JIRA/ADR-style ticket id: DEV-69, ADR-0004, PROJ-123."""

_PATH_RE = re.compile(r"^(?:/|\./|\.\./)[^\s]+$|^[\w.-]+(?:/[\w.-]+)+$")
"""A filesystem/route path: /admin/signups, ./scripts, docs/self-hosting.md."""

_FILENAME_RE = re.compile(r"^[\w-]+(?:\.[\w-]+)*\.[A-Za-z0-9]{1,6}$")
"""A dotted filename: operations.md, fly.dev.toml, smoke_mcp.py."""

# End-anchored form of ``_DOMAIN_RE`` (the TLD must be the FINAL segment) for the
# filename carve-out. This is what keeps the ``maxime.team`` (bare domain →
# person→product retype) case a NON-structural name while still treating a config
# file like ``fly.dev.toml`` (whose ``.dev`` is mid-name, not the suffix) as
# structural. Anchoring to the end resolves the ambiguity a plain ``.search``
# would create — ``fly.dev.toml`` contains a TLD-shaped segment but is a file.
_DOMAIN_SUFFIX_RE = re.compile(
    r"\.(team|me|com|ai|io|app|dev|net|org|co|xyz|de|ch|eu)$",
    re.IGNORECASE,
)


def is_structural_name(name: str) -> bool:
    """True when ``name`` is a structural token (ticket id, path, or config
    filename) rather than a real entity whose kind is worth reviewing.

    I6-safe: this is purely a REVIEW-PROPOSAL suppressor. It assigns/forces NO
    kind, introduces NO enum, and leaves ``kind_observations`` and the entity
    itself untouched — the Schema-Evolver and manual retype/retract stay fully
    available. It only decides "the operator doesn't need to be nagged to pick a
    kind for a file path". Same category as the extractor's evidence gate: a
    lexical-shape judgment about what deserves human attention, not an ontology.

    The ``_DOMAIN_SUFFIX_RE`` carve-out is load-bearing: a bare domain used as a
    name (``maxime.team``) stays NON-structural so its person→product retype
    still surfaces; a config filename that merely contains a TLD-shaped segment
    (``fly.dev.toml``) is still structural.

    KNOWN, ACCEPTED false-positive class: real names shaped like a ticket id or
    a dotted/slashed filename classify as structural — ``Node.js``, ``Next.js``,
    ``GPT-4``, ``COVID-19``, ``TCP/IP``, ``F-16``. The harm is bounded to
    review-proposal suppression (the entity, its ``kind_observations``, and
    manual retype/retract are all intact), so we deliberately do NOT complicate
    the regexes to chase these — the miss is "no auto-nag to pick a kind", which
    the operator can always do by hand.
    """
    name = name.strip()
    if not name:
        return False
    if _TICKET_ID_RE.match(name) or _PATH_RE.match(name):
        return True
    # A dotted filename is structural UNLESS its final segment is a real TLD (a
    # bare domain used as a name — the load-bearing maxime.team carve-out).
    return bool(_FILENAME_RE.match(name)) and not _DOMAIN_SUFFIX_RE.search(name)


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


# Merges by these authors are operator-made (or operator-directed renames), so
# they're already decided — only AUTOMATIC merges go to review.
_OPERATOR_AUTHORS = ("operator",)
_OPERATOR_AUTHOR_PREFIXES = ("manual:",)


def _is_auto_merge(merged_by: str) -> bool:
    if merged_by in _OPERATOR_AUTHORS:
        return False
    return not any(merged_by.startswith(p) for p in _OPERATOR_AUTHOR_PREFIXES)


def find_cross_kind_auto_merges(
    conn: sqlite3.Connection,
) -> list[tuple[str, dict[str, Any], str, float]]:
    """Automatic merges that crossed a kind boundary — the cluster decisions
    the system made *for* the operator and never asked about.

    Returns ``(from_entity_id, detail, evidence, confidence)`` for each
    ``entity_merges`` row where the two entities have different kinds and the
    merge was made by an automatic agent (not the operator, not a manual
    rename). ``from_entity_id`` keys the proposal — each auto-merge is reviewed
    once. ``detail`` carries everything the surface needs to ask the question
    and apply a correction (the canonical ``into`` entity + the kind it picked).
    """
    # ADR-0003 Phase 2: compare CURRENT kinds through the resolution view —
    # a merge whose kind conflict the operator already fixed with an
    # assignment row stops surfacing as cross-kind.
    rows = conn.execute(
        """
        SELECT mg.from_entity_id, mg.into_entity_id, mg.merged_by, mg.confidence,
               a.canonical_name AS from_name, ka.kind_slug AS from_kind,
               b.canonical_name AS into_name, kb.kind_slug AS into_kind
        FROM entity_merges mg
        JOIN entities a ON a.id = mg.from_entity_id
        JOIN entities b ON b.id = mg.into_entity_id
        JOIN entity_current_kind_v1 ka ON ka.entity_id = a.id
        JOIN entity_current_kind_v1 kb ON kb.entity_id = b.id
        WHERE ka.kind_slug != kb.kind_slug
          AND b.id NOT IN (SELECT entity_id FROM entity_retractions)
        """
    ).fetchall()
    out: list[tuple[str, dict[str, Any], str, float]] = []
    for r in rows:
        if not _is_auto_merge(r["merged_by"]):
            continue
        detail = {
            "from_name": r["from_name"],
            "from_kind": r["from_kind"],
            "into_entity_id": r["into_entity_id"],
            "into_name": r["into_name"],
            "merged_kind": r["into_kind"],
            "merged_by": r["merged_by"],
        }
        evidence = (
            f"'{r['merged_by']}' auto-merged '{r['from_name']}' ({r['from_kind']}) "
            f"into '{r['into_name']}' ({r['into_kind']}) — kind chosen without review"
        )
        conf = r["confidence"] if r["confidence"] is not None else 0.5
        out.append((r["from_entity_id"], detail, evidence, conf))
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
    """Insert a proposal only when no row of ANY status exists for
    (kind, entity_id). Returns True if a new row landed.

    P1-1: with the partial unique index (open rows only), a plain
    ``INSERT OR IGNORE`` would no longer block on DECIDED history, so the audit
    would re-file every decided-but-still-cross-kind merge each 12h cycle. The
    explicit ``NOT EXISTS`` preserves the exact prior semantics: a row of any
    status for (kind, entity_id) blocks — the audit never re-opens a closed
    question. That durable memory is why decided retype/merge/merge_review rows
    are KEPT (only edge_review rows age out; their durable guard is the
    append-only edge_reviews table). The partial index remains the race
    backstop for two concurrent inserts of the same open proposal."""
    cur = conn.execute(
        """
        INSERT INTO proposed_corrections (
            id, kind, entity_id, detail, evidence, confidence, tier,
            detected_by, detected_at, status
        )
        SELECT ?, ?, ?, ?, ?, ?, 'review', ?, ?, 'proposed'
        WHERE NOT EXISTS (
            SELECT 1 FROM proposed_corrections WHERE kind = ? AND entity_id = ?
        )
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
            kind,
            entity_id,
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
        # Current entities only (not merged away), with their CURRENT kind
        # through the resolution view (ADR-0003 Phase 2) — a retyped entity
        # is audited under its assigned kind, not its immutable initial one.
        rows = conn.execute(
            """
            SELECT e.id, e.canonical_name, ck.kind_slug AS kind
            FROM entities e
            JOIN entity_current_kind_v1 ck ON ck.entity_id = e.id
            LEFT JOIN entity_merges m ON m.from_entity_id = e.id
            WHERE m.id IS NULL
              AND e.id NOT IN (SELECT entity_id FROM entity_retractions)
            """,
        ).fetchall()
        entities = [(r["id"], r["canonical_name"], r["kind"]) for r in rows]

        stats: dict[str, Any] = {
            "scanned": len(entities),
            "retype_proposals": 0,
            "merge_review_proposals": 0,
            "suppressed_structural": 0,
        }
        with conn:
            for eid, name, kind in entities:
                # C — structural-junk suppression (I6-safe). A file path / ticket
                # id / config filename doesn't need a human to pick its kind; skip
                # the proposal only. The entity + its kind_observations stand, and
                # manual retype/retract stay available.
                if is_structural_name(name):
                    stats["suppressed_structural"] += 1
                    continue
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
            for from_id, detail, evidence, conf in find_cross_kind_auto_merges(conn):
                # Same suppression on the merge-review side — but only when BOTH
                # names are structural. A REAL entity auto-merged INTO a
                # structural name (e.g. "afair" -> "operations.md") is arguably
                # the merge most worth reviewing, so an OR here would wrongly
                # silence it; require AND so at least one real name still nags.
                if is_structural_name(detail["from_name"]) and is_structural_name(
                    detail["into_name"]
                ):
                    stats["suppressed_structural"] += 1
                    continue
                if _insert_proposal(
                    conn,
                    kind="merge_review",
                    entity_id=from_id,
                    detail=detail,
                    evidence=evidence,
                    confidence=conf,
                    now=now,
                ):
                    stats["merge_review_proposals"] += 1

        if stats["retype_proposals"] or stats["merge_review_proposals"]:
            log.info("entity_audit.proposals", **stats)
        return stats
