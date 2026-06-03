"""Salience worker — Phase 2 "what matters" scorer.

Computes a per-event salience score in [0, 1] that summarizes how
likely an event is to matter for future recall, mode-switching, or
consolidation. The score is derived from cheap, side-effect-free
substrate signals — no LLM call — so it scales linearly with the
event count without burning quota.

Score components
================
salience = clamp_0_1(sum_of_weighted_components):

  * entity_density:   how many canonical entities the event mentions
                      (linear up to 5, plateaus after) — events with
                      named people / projects / places matter more than
                      generic prose.
  * link_density:     how many bind-links point at this event from
                      other events (semantic neighborhood). Plateaus
                      at 10.
  * has_conflict:     was this event judged by the conflict-resolver?
                      0.1 bump regardless of verdict — a contested
                      claim is salient by definition.
  * type_hint_signal: certain user-provided type_hints (decision,
                      preference, fact, constitution) carry an
                      inherent salience baseline.
  * is_compound:      compound events represent deliberate bundling
                      (meeting = transcript+slides+image) → 0.1 bump.
  * recency_decay:    pure-recency contribution falls off over 30
                      days. Lets recent events surface more easily
                      without making old events permanently invisible.

The interpretation row carries both the final score AND the
components dict so future readers (mode-switching agent, debug UI)
can see the breakdown without rerunning the computation.

Producer string: "salience:v0".

Schema impact: none. Salience reuses the existing interpretations
table — one row per scored event with extraction.salience and
extraction.salience_components.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from ..substrate import pipeline_events as pe
from ..substrate.events import read_event_by_id
from .cold_path import ColdPathWorker
from .interpretation import write_interpretation
from .tunable_registry import TunableRegistry

if TYPE_CHECKING:
    import sqlite3

    from ..settings import Settings

log = structlog.get_logger(__name__)

SALIENCE_VERSION = 0
SALIENCE_PRODUCED_BY = "salience:v0"
SALIENCE_KIND_FILTER = ("remember", "observe")
"""Score remember + observe events. Skip consolidations + invalidations
— those are derived, not first-class observations."""


# Component weights — sourced from the TunableRegistry so the
# self-improvement tuner can adjust them. Static defaults still live
# here as a fallback (and are mirrored as the registry default), so
# the worker stays runnable even if the registry layer fails. The
# canonical values come from
# ``TunableRegistry.get("salience", "component_weights")``.
_DEFAULT_WEIGHTS: dict[str, float] = {
    "entity_density": 0.25,
    "link_density": 0.20,
    "has_conflict": 0.10,
    "type_hint_bump": 0.15,
    "is_compound": 0.10,
    "recency": 0.20,
}

# Per-event budget per cycle — caps the worst-case work the scheduler
# triggers in one tick. With 200 unscored events per cycle and one
# substrate row + a few SELECTs per event, a run stays under ~5s
# even on a busy vault.
SALIENCE_BATCH_LIMIT = 200

# Recency decay — full weight at age 0, zero weight at age 30 days.
RECENCY_DECAY_DAYS = 30

# Type-hints that carry an inherent salience bump. Free-text (per I6
# no fixed ontology) — readers don't depend on this list staying
# stable, it's just a heuristic for cheap salience boost.
HIGH_SIGNAL_TYPE_HINTS: frozenset[str] = frozenset(
    {
        "decision",
        "preference",
        "fact",
        "constitution",
        "principle",
        "rule",
        "deadline",
        "commitment",
        "promise",
        "complaint",
        "concern",
        "insight",
        "discovery",
    }
)


class SalienceWorker(ColdPathWorker):
    """Score every unscored event for salience.

    Cheap and idempotent: skips events that already have a salience:v0
    interpretation row. Each cycle processes up to
    :data:`SALIENCE_BATCH_LIMIT` events; the scheduler's next tick
    picks up wherever this one stopped.
    """

    name = "salience"
    interval_seconds = 300  # every 5 min

    def run(self, conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
        _ = settings  # not used yet — kept for the Worker contract
        # Construct the registry per-cycle. Reads are connection-scoped
        # and cached for the lifetime of this run() call; the next cycle
        # gets a fresh read so any tuner promote that happened in the
        # meantime takes effect.
        registry = TunableRegistry(conn)
        weights = registry.get("salience", "component_weights")
        stats: dict[str, Any] = {
            "candidates": 0,
            "scored": 0,
            "skipped_already_scored": 0,
            "skipped_event_missing": 0,
            "weights_snapshot": weights,
        }

        # Find events without a salience interpretation. Bounded by
        # SALIENCE_BATCH_LIMIT so a backlog never wedges one cycle.
        rows = conn.execute(
            """
            SELECT e.id
            FROM events e
            WHERE e.kind IN ('remember', 'observe')
              AND NOT EXISTS (
                  SELECT 1 FROM interpretations i
                  WHERE i.event_hash = e.content_hash
                    AND i.produced_by = ?
              )
            ORDER BY e.created_at DESC
            LIMIT ?
            """,
            (SALIENCE_PRODUCED_BY, SALIENCE_BATCH_LIMIT),
        ).fetchall()
        stats["candidates"] = len(rows)

        for row in rows:
            event_id = row["id"]
            event = read_event_by_id(conn, event_id)
            if event is None:
                stats["skipped_event_missing"] += 1
                continue
            score, components = score_event(conn, event, weights=weights)
            extraction: dict[str, Any] = {
                "salience": score,
                "salience_components": components,
                "status": "success",
            }
            try:
                write_interpretation(
                    conn,
                    event=event,
                    version=SALIENCE_VERSION,
                    produced_by=SALIENCE_PRODUCED_BY,
                    extraction=extraction,
                )
            except Exception as e:  # idempotency contract should make this rare
                log.warning(
                    "salience.write_failed",
                    event_id=event_id,
                    error=str(e),
                )
                continue
            stats["scored"] += 1
            pe.record(
                conn,
                event_id=event_id,
                event_hash=event.content_hash,
                stage="salience.scored",
                producer=SALIENCE_PRODUCED_BY,
                detail=f"score={score:.3f}",
            )

        log.info("salience.cycle", **stats)
        return stats


def score_event(
    conn: sqlite3.Connection,
    event: Any,
    *,
    weights: dict[str, float] | None = None,
) -> tuple[float, dict[str, float]]:
    """Compute salience + component breakdown for one event.

    Pure function over substrate state — safe to call from anywhere
    that has a read connection. Returns ``(score, components)`` where
    ``score`` is the clamped final salience and ``components`` is the
    per-signal contribution dict (for audit / future tuning).

    Weights default to the static dict in this module. Production
    code paths (the SalienceWorker) read them from the TunableRegistry
    and pass them in; the default keeps direct callers (tests,
    backfill scripts) working without registry plumbing.
    """
    w = weights if weights is not None else _DEFAULT_WEIGHTS
    payload = event.payload or {}

    entity_density = _entity_density_score(conn, event.content_hash)
    link_density = _link_density_score(conn, event.content_hash)
    has_conflict = _has_conflict_score(conn, event.content_hash)
    type_hint_bump = _type_hint_score(payload.get("type_hint"))
    is_compound = (
        1.0 if isinstance(payload, dict) and payload.get("content_type") == "compound" else 0.0
    )
    recency = _recency_score(event.created_at)

    components = {
        "entity_density": entity_density,
        "link_density": link_density,
        "has_conflict": has_conflict,
        "type_hint_bump": type_hint_bump,
        "is_compound": is_compound,
        "recency": recency,
    }
    score = (
        w["entity_density"] * entity_density
        + w["link_density"] * link_density
        + w["has_conflict"] * has_conflict
        + w["type_hint_bump"] * type_hint_bump
        + w["is_compound"] * is_compound
        + w["recency"] * recency
    )
    score = max(0.0, min(1.0, score))
    return score, components


def _entity_density_score(conn: sqlite3.Connection, content_hash: str) -> float:
    """0..1 — linear up to 5 distinct canonical entities, then 1.0."""
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT entity_id) AS n
        FROM entity_mentions
        WHERE event_hash = ?
        """,
        (content_hash,),
    ).fetchone()
    n = row["n"] if row else 0
    return min(1.0, n / 5.0)


def _link_density_score(conn: sqlite3.Connection, content_hash: str) -> float:
    """0..1 — based on bind-links this event has to neighbors.

    Bind links live as JSON inside the binder interpretation row
    (``produced_by = 'binder:v0'``). We count the entries in this
    event's links array. Plateaus at 10 neighbors — beyond that the
    event is functionally a hub and the salience signal saturates.

    Outgoing-link counting (this) is symmetric with the binder's
    pairing logic: when X gets linked to Y, both X's and Y's binder
    rows record the link. So this counter approximates connectivity
    in both directions.
    """
    import json

    from .binder import BINDER_PRODUCED_BY

    row = conn.execute(
        """
        SELECT extraction FROM interpretations
        WHERE event_hash = ? AND produced_by = ?
        ORDER BY produced_at DESC
        LIMIT 1
        """,
        (content_hash, BINDER_PRODUCED_BY),
    ).fetchone()
    if row is None:
        return 0.0
    try:
        data = json.loads(row["extraction"])
        links = data.get("links", []) if isinstance(data, dict) else []
        n = len(links) if isinstance(links, list) else 0
    except (ValueError, TypeError):
        return 0.0
    return min(1.0, n / 10.0)


def _has_conflict_score(conn: sqlite3.Connection, content_hash: str) -> float:
    """1.0 if any conflict_resolver verdict touches this event, else 0.0.

    A contested claim is salient by definition — the user (or some
    later memory) will care about reconciling it."""
    row = conn.execute(
        """
        SELECT 1
        FROM interpretations
        WHERE (event_hash = ? OR produced_by LIKE 'conflict_resolver:v0:' || ?)
          AND produced_by LIKE 'conflict_resolver:%'
        LIMIT 1
        """,
        (content_hash, content_hash),
    ).fetchone()
    return 1.0 if row is not None else 0.0


def _type_hint_score(type_hint: object) -> float:
    """1.0 if the user-provided type_hint matches a high-signal label,
    else 0.0. Free-text per I6, but a small allowlist catches the
    obvious "this is important" hints without an LLM call."""
    if not isinstance(type_hint, str):
        return 0.0
    return 1.0 if type_hint.lower().strip() in HIGH_SIGNAL_TYPE_HINTS else 0.0


def _recency_score(created_at_iso: str) -> float:
    """Linear decay from 1.0 at age 0 to 0.0 at RECENCY_DECAY_DAYS."""
    try:
        created = datetime.fromisoformat(created_at_iso)
    except ValueError:
        return 0.0
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    age = datetime.now(UTC) - created
    if age <= timedelta(0):
        return 1.0
    if age >= timedelta(days=RECENCY_DECAY_DAYS):
        return 0.0
    return 1.0 - (age.total_seconds() / (RECENCY_DECAY_DAYS * 86400))


# ── reader helpers used by the mode-switching agent ────────────────────────


def read_recent_salience(
    conn: sqlite3.Connection, *, limit: int = 100
) -> list[tuple[str, float, str]]:
    """Return ``[(event_id, salience, created_at)]`` for the last ``limit``
    salience-scored events. Used by mode-switching to compute the
    rolling salience signal that drives CEN↔DMN transitions."""
    rows = conn.execute(
        """
        SELECT e.id AS event_id, e.created_at, i.extraction
        FROM interpretations i
        JOIN events e ON e.content_hash = i.event_hash
        WHERE i.produced_by = ?
        ORDER BY e.created_at DESC
        LIMIT ?
        """,
        (SALIENCE_PRODUCED_BY, limit),
    ).fetchall()
    out: list[tuple[str, float, str]] = []
    for row in rows:
        try:
            import json

            extraction = json.loads(row["extraction"])
            score = float(extraction.get("salience", 0.0))
        except (ValueError, TypeError):
            score = 0.0
        out.append((row["event_id"], score, row["created_at"]))
    return out
