"""EntityCanonicalizer cold-path worker (Phase 4 Track 1, Stage 2).

Closes the loop on the context-mix problem: the Extractor already pulls
``entities`` and ``relations`` per event, but those mentions are scoped
to a single event's interpretation. The canonicalizer reads recent
extractions, decides which surface forms refer to the same real-world
entity, and materializes the cross-event graph in the substrate
(``entities``, ``entity_mentions``, ``entity_edges``).

Three-stage match per surface form
----------------------------------
1. **Exact** — case-insensitive name + same kind lookup against
   ``entities``. If it hits, we link the mention and move on. No LLM call.
2. **LLM judgment** — only invoked when no exact match exists AND the
   substrate already has at least one candidate of the same kind.
   Candidate pool pruned by lexical similarity (``difflib``) to top-K
   so the LLM sees a small, relevant menu. Default model is Haiku
   (decision #3); confidence < 0.7 triggers a Sonnet escalation that
   re-judges the same input with a stronger model.
3. **New entity** — no exact match, no LLM match → write a new canonical
   entity (provisional 0.5 confidence) and link the mention.

Cascade invalidation
--------------------
Decision #5: ``remember(content, invalidates=[hash])`` writes an
``invalidate`` event in the substrate. Each cycle, this worker scans
for invalidate events that haven't been cascaded yet, finds every
entity_edge whose ``source_event_id`` matches the invalidated target,
and writes an ``edge_invalidations`` row. The cascade marker itself is
an interpretation row ``produced_by='entity_canonicalizer:cascaded:vN'``
so re-runs are idempotent without a separate cursor table.

Budget + rate-limit
-------------------
Hard caps per cycle (decision #7, lessons from ConflictResolver):
    MAX_EVENTS_PER_CYCLE        — how many events to canonicalize
    MAX_LLM_CALLS_PER_CYCLE     — total LLM-judgment budget
    INTER_CALL_SLEEP_SECONDS    — pause between LLM calls
The scheduler runs this worker every 120s (offset from ConflictResolver
to avoid request bursts).
"""

from __future__ import annotations

import difflib
import json
import time
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel

from ..substrate.entities import (
    Entity,
    find_edges_for_source_event,
    find_entity_by_name,
    read_entity_by_id,
    write_edge_invalidation,
    write_entity,
    write_entity_edge,
    write_entity_mention,
)
from ..substrate.events import read_event_by_hash
from .cold_path import ColdPathWorker
from .interpretation import write_interpretation
from .invalidation import INVALIDATE_KIND
from .llm import LLMError, call_tool

if TYPE_CHECKING:
    import sqlite3

    from ..settings import Settings
    from ..substrate.events import Event

log = structlog.get_logger(__name__)


# ── version + producer markers ────────────────────────────────────────────

CANONICALIZER_VERSION = 1
CANONICALIZER_PRODUCED_BY = "entity_canonicalizer:v0"
"""Worker identity stamped into entity_mentions.canonicalized_by and into
the cascade-marker interpretation rows. Bumping the version is how we
re-canonicalize old events when the matcher itself improves (I7)."""

CASCADE_PRODUCED_BY = "entity_canonicalizer:cascaded:v1"
"""Marker interpretation row for invalidate events whose cascade has run.
Lives in the interpretations table so re-runs are idempotent without a
separate cursor."""

NO_MENTIONS_PRODUCED_BY = "entity_canonicalizer:no_mentions:v1"
"""Marker for events whose extractor output had no canonicalizable entities
(empty names, all filtered, etc.). Without this marker the
``_find_uncanonicalized_events`` query would re-surface these events every
cycle in an infinite loop, because the NOT EXISTS check only filters by
entity_mentions presence. The marker tells the worker "we already looked,
there was nothing to write" without violating I2 (it's an interpretation
row, not a mutation)."""


# ── budget knobs ──────────────────────────────────────────────────────────

MAX_EVENTS_PER_CYCLE = 10
"""How many uncanonicalized events to process per cycle. With ~3 entities
per event and up to one LLM call per entity, 10 events x3 = 30 potential
LLM calls — capped further by MAX_LLM_CALLS_PER_CYCLE below."""

MAX_CASCADES_PER_CYCLE = 8
"""How many uncascaded invalidate events to process per cycle. Cascades
involve only SQL writes (no LLM), so the cap is generous."""

MAX_LLM_CALLS_PER_CYCLE = 8
"""Hard ceiling on LLM judgments per cycle. Same shape as ConflictResolver's
budget — keeps us under the per-minute org rate limit even when the warm-
path Extractor and other cold-path workers are also firing."""

INTER_CALL_SLEEP_SECONDS = 3.0
"""Pause between LLM calls inside a single cycle. With 8 calls x3s spacing
the cycle runs ~25s, leaving headroom for the Extractor."""

CANDIDATE_POOL_SIZE = 8
"""How many existing entities of the same kind to show the LLM as
candidates. Pruned from the full pool by lexical similarity to the
surface form so the LLM sees a small, relevant menu."""

SONNET_ESCALATION_THRESHOLD = 0.7
"""Decision #3: if Haiku returns a verdict with confidence below this,
re-judge with Sonnet using the same prompt + candidates."""

PROVISIONAL_NEW_ENTITY_CONFIDENCE = 0.5
"""Confidence stamped on entities created without LLM confirmation —
provisional, may be raised when the canonicalizer next sees the same
surface form (no — entities are immutable, so 'raising' means the LLM
later issues a merge into a higher-confidence canonical)."""


# ── LLM tool schema for the match-judgment call ───────────────────────────

_MATCH_TOOL_NAME = "record_entity_match"
_MATCH_TOOL_DESCRIPTION = (
    "Decide whether a surface form refers to an existing canonical entity "
    "or to a new entity. Pick at most one candidate."
)
_MATCH_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "matched_entity_id": {
            "type": ["string", "null"],
            "description": (
                "ID of the candidate entity this surface form refers to, "
                "OR null if none of the candidates match (the system will "
                "create a new canonical entity)."
            ),
        },
        "reason": {
            "type": "string",
            "description": "One short sentence explaining the verdict.",
        },
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": (
                "Your self-assessment, 0=guess, 1=explicit. Confidence below "
                "0.7 will be re-judged by a stronger model."
            ),
        },
    },
    "required": ["matched_entity_id", "reason", "confidence"],
}

_MATCH_SYSTEM_PROMPT = """\
You are an entity-canonicalization judge for a personal memory vault.
Given a surface form (e.g., "Sajinth") plus the surrounding text from the
event where it appeared, decide whether it refers to one of the candidate
entities the system already knows about, or to a NEW entity not yet
present in the candidate list.

Guidance:
- Same person/org/place referenced by a different spelling or variant
  (case, nickname, abbreviation, partial name) → match the candidate.
- Different person/org/place that happens to share a name → no match;
  return null and explain why in the reason.
- When in doubt, prefer null (a new entity) — false merges are harder to
  undo than false splits. The system's Schema-Evolver later cleans up.
- Use the record_entity_match tool exactly once.
"""


# ── LLM verdict model ─────────────────────────────────────────────────────


class _MatchVerdict(BaseModel):
    matched_entity_id: str | None
    reason: str
    confidence: float


# ── worker ────────────────────────────────────────────────────────────────


class EntityCanonicalizer(ColdPathWorker):
    """Cold-path worker that materializes the entity graph from extractions."""

    name = "entity_canonicalizer"
    interval_seconds = 120  # decision #7 — offset from ConflictResolver

    def run(self, conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "events_canonicalized": 0,
            "entities_created": 0,
            "entities_matched_exact": 0,
            "entities_matched_llm": 0,
            "edges_created": 0,
            "edges_skipped_unresolved": 0,
            "invalidations_cascaded": 0,
            "edges_invalidated": 0,
            "llm_calls": 0,
            "llm_errors": 0,
            "sonnet_escalations": 0,
        }

        model = settings.extractor_model
        api_key = _api_key_for_model(model, settings)
        sonnet_model = _sonnet_for(model)
        llm_budget = MAX_LLM_CALLS_PER_CYCLE
        last_llm_call: float | None = None

        # Phase A — canonicalize new events.
        events_with_extractions = _find_uncanonicalized_events(conn, MAX_EVENTS_PER_CYCLE)
        for event, extraction in events_with_extractions:
            result, last_llm_call = _canonicalize_one_event(
                conn,
                event=event,
                extraction=extraction,
                model=model,
                sonnet_model=sonnet_model,
                api_key=api_key,
                llm_budget=llm_budget,
                last_llm_call=last_llm_call,
            )
            stats["events_canonicalized"] += 1
            stats["entities_created"] += result["created"]
            stats["entities_matched_exact"] += result["matched_exact"]
            stats["entities_matched_llm"] += result["matched_llm"]
            stats["edges_created"] += result["edges_created"]
            stats["edges_skipped_unresolved"] += result["edges_skipped"]
            stats["llm_calls"] += result["llm_calls"]
            stats["llm_errors"] += result["llm_errors"]
            stats["sonnet_escalations"] += result["sonnet_escalations"]
            llm_budget -= result["llm_calls"]
            # Idle-loop guard: if THIS pass wrote no mentions for the
            # event (extractor returned entities-list but all filtered —
            # empty names, malformed shapes), stamp a marker so the
            # NOT EXISTS query stops surfacing this event next cycle.
            if (
                result["created"] == 0
                and result["matched_exact"] == 0
                and result["matched_llm"] == 0
            ):
                write_interpretation(
                    conn,
                    event=event,
                    version=CANONICALIZER_VERSION,
                    produced_by=NO_MENTIONS_PRODUCED_BY,
                    extraction={
                        "status": "success",
                        "content_type": "no_mentions_marker",
                        "reason": "extractor entities present but all filtered (empty names, invalid shapes)",
                    },
                )
            if llm_budget <= 0:
                # Drain remaining events with exact-only matching by leaving
                # llm_budget at 0; the helper handles that mode internally.
                pass

        # Phase B — cascade pending invalidations.
        for invalidate_event in _find_uncascaded_invalidations(conn, MAX_CASCADES_PER_CYCLE):
            edges_invalidated = _cascade_invalidation(conn, invalidate_event)
            stats["invalidations_cascaded"] += 1
            stats["edges_invalidated"] += edges_invalidated
            # Mark this invalidate event as cascaded so we don't re-process
            # it next cycle. The marker lives in the interpretations table
            # alongside other producer-keyed rows.
            write_interpretation(
                conn,
                event=invalidate_event,
                version=CANONICALIZER_VERSION,
                produced_by=CASCADE_PRODUCED_BY,
                extraction={
                    "status": "success",
                    "content_type": "cascade_marker",
                    "edges_invalidated": edges_invalidated,
                },
            )

        return stats


# ── Phase A: canonicalize new events ──────────────────────────────────────


def _find_uncanonicalized_events(
    conn: sqlite3.Connection, max_events: int
) -> list[tuple[Event, dict[str, Any]]]:
    """Events whose latest extractor interpretation has entities/relations
    but no entity_mentions row yet for this event_hash.

    Joins interpretations (LIKE 'extractor:%') with a NOT EXISTS against
    entity_mentions on event_hash. Returns oldest-first so the graph
    catches up to recent activity in temporal order.
    """
    rows = conn.execute(
        """
        SELECT i.event_id, i.event_hash, i.extraction, e.created_at
        FROM interpretations i
        JOIN events e ON e.id = i.event_id
        WHERE i.produced_by LIKE 'extractor:%'
          AND NOT EXISTS (
              SELECT 1 FROM entity_mentions m
              WHERE m.event_hash = i.event_hash
          )
          AND NOT EXISTS (
              SELECT 1 FROM interpretations m
              WHERE m.event_hash = i.event_hash
                AND m.produced_by = ?
          )
        ORDER BY e.created_at ASC
        LIMIT ?
        """,
        (NO_MENTIONS_PRODUCED_BY, max_events),
    ).fetchall()

    out: list[tuple[Event, dict[str, Any]]] = []
    seen_hashes: set[str] = set()
    for row in rows:
        if row["event_hash"] in seen_hashes:
            continue  # multiple extractor rows per event possible (#retryN); dedupe
        seen_hashes.add(row["event_hash"])
        extraction = json.loads(row["extraction"])
        if extraction.get("status") != "success":
            continue  # failed extractions don't have usable entities
        if not extraction.get("entities") and not extraction.get("relations"):
            # Nothing to canonicalize, but mark as "processed" with a
            # no-op mention-less marker so we don't re-scan forever.
            # Cheapest: skip. The NOT EXISTS query will keep returning
            # this row, but the canonicalize-one-event helper is fast
            # for empty inputs.
            pass
        event = read_event_by_hash(conn, row["event_hash"])
        if event is None:
            continue
        out.append((event, extraction))
    return out


def _canonicalize_one_event(
    conn: sqlite3.Connection,
    *,
    event: Event,
    extraction: dict[str, Any],
    model: str,
    sonnet_model: str | None,
    api_key: str | None,
    llm_budget: int,
    last_llm_call: float | None,
) -> tuple[dict[str, int], float | None]:
    """Resolve entities + relations for one event into substrate rows.

    Returns (stats_delta, updated_last_llm_call_time).
    """
    stats = {
        "created": 0,
        "matched_exact": 0,
        "matched_llm": 0,
        "edges_created": 0,
        "edges_skipped": 0,
        "llm_calls": 0,
        "llm_errors": 0,
        "sonnet_escalations": 0,
    }

    # Stable map from raw surface form → resolved Entity for this event.
    # Used both to dedupe within-event repeats AND to resolve relation
    # subjects/objects against entities we just canonicalized.
    resolved: dict[str, Entity] = {}

    # ── entities[]
    raw_entities = extraction.get("entities") or []
    if not isinstance(raw_entities, list):
        raw_entities = []
    for entity_dict in raw_entities:
        if not isinstance(entity_dict, dict):
            continue
        surface_form = str(entity_dict.get("name") or "").strip()
        kind_raw = str(entity_dict.get("type") or "other").strip()
        kind = _normalize_kind(kind_raw)
        if not surface_form:
            continue
        if surface_form in resolved:
            continue  # already handled this surface form in the same event

        # Stage 1: exact match.
        existing = find_entity_by_name(conn, canonical_name=surface_form, kind=kind)
        if existing:
            best = max(existing, key=lambda e: e.confidence)
            resolved[surface_form] = best
            stats["matched_exact"] += 1
            write_entity_mention(
                conn,
                entity_id=best.id,
                event_id=event.id,
                event_hash=event.content_hash,
                surface_form=surface_form,
                canonicalized_by=CANONICALIZER_PRODUCED_BY,
                match_method="exact",
                confidence=1.0,
            )
            continue

        # Stage 2: LLM judgment against candidate pool (if budget allows).
        candidates = _candidate_pool(
            conn, surface_form=surface_form, kind=kind, limit=CANDIDATE_POOL_SIZE
        )
        if candidates and (llm_budget - stats["llm_calls"]) > 0:
            try:
                last_llm_call = _maybe_sleep(last_llm_call)
                verdict = _llm_judge_match(
                    surface_form=surface_form,
                    surrounding_text=_event_surrounding_text(event, extraction),
                    candidates=candidates,
                    model=model,
                    api_key=api_key,
                )
                stats["llm_calls"] += 1

                # Sonnet escalation on low-confidence verdicts.
                if (
                    verdict.confidence < SONNET_ESCALATION_THRESHOLD
                    and sonnet_model is not None
                    and (llm_budget - stats["llm_calls"]) > 0
                ):
                    last_llm_call = _maybe_sleep(last_llm_call)
                    verdict = _llm_judge_match(
                        surface_form=surface_form,
                        surrounding_text=_event_surrounding_text(event, extraction),
                        candidates=candidates,
                        model=sonnet_model,
                        api_key=api_key,
                    )
                    stats["llm_calls"] += 1
                    stats["sonnet_escalations"] += 1

                if verdict.matched_entity_id is not None:
                    matched = read_entity_by_id(conn, verdict.matched_entity_id)
                    if matched is not None:
                        resolved[surface_form] = matched
                        stats["matched_llm"] += 1
                        write_entity_mention(
                            conn,
                            entity_id=matched.id,
                            event_id=event.id,
                            event_hash=event.content_hash,
                            surface_form=surface_form,
                            canonicalized_by=CANONICALIZER_PRODUCED_BY,
                            match_method="llm",
                            confidence=verdict.confidence,
                        )
                        continue
            except LLMError as e:
                log.warning(
                    "entity_canonicalizer.llm_error",
                    surface_form=surface_form,
                    error=str(e),
                )
                stats["llm_errors"] += 1
                # Fall through to "create new"

        # Stage 3: new canonical entity.
        new_entity = write_entity(
            conn,
            canonical_name=surface_form,
            kind=kind,
            created_by=CANONICALIZER_PRODUCED_BY,
            source_event_id=event.id,
            confidence=PROVISIONAL_NEW_ENTITY_CONFIDENCE,
        )
        resolved[surface_form] = new_entity
        stats["created"] += 1
        write_entity_mention(
            conn,
            entity_id=new_entity.id,
            event_id=event.id,
            event_hash=event.content_hash,
            surface_form=surface_form,
            canonicalized_by=CANONICALIZER_PRODUCED_BY,
            match_method="new",
            confidence=PROVISIONAL_NEW_ENTITY_CONFIDENCE,
        )

    # ── relations[] — emit edges only when both ends resolve to entities
    # we've seen in THIS event. We don't try to look up entities by name
    # across the full vault here; cross-event linking is implicit through
    # the canonical-name match in Stage 1 (same name → same entity_id).
    raw_relations = extraction.get("relations") or []
    if not isinstance(raw_relations, list):
        raw_relations = []
    for relation_dict in raw_relations:
        if not isinstance(relation_dict, dict):
            continue
        subj = str(relation_dict.get("subject") or "").strip()
        pred = str(relation_dict.get("predicate") or "").strip()
        obj = str(relation_dict.get("object") or "").strip()
        if not (subj and pred and obj):
            continue
        # Resolve subject + object against the entities we just canonicalized.
        # If either side wasn't extracted as an entity, fall back to a name
        # lookup across the substrate. Predicate stays emergent per
        # decision #2 — no canonical predicate vocabulary.
        subj_entity = resolved.get(subj) or _lookup_any_kind(conn, subj)
        obj_entity = resolved.get(obj) or _lookup_any_kind(conn, obj)
        if subj_entity is None or obj_entity is None:
            stats["edges_skipped"] += 1
            continue
        if subj_entity.id == obj_entity.id:
            # Self-edges would be allowed by the schema but rarely useful;
            # skip them to keep the graph clean.
            stats["edges_skipped"] += 1
            continue
        edge = write_entity_edge(
            conn,
            subject_id=subj_entity.id,
            predicate=pred,
            object_id=obj_entity.id,
            source_event_id=event.id,
            discovered_by=CANONICALIZER_PRODUCED_BY,
            confidence=0.8,
        )
        if edge is not None:
            stats["edges_created"] += 1

    return stats, last_llm_call


def _candidate_pool(
    conn: sqlite3.Connection, *, surface_form: str, kind: str, limit: int
) -> list[Entity]:
    """Pre-LLM pruning: top-K existing entities of the same kind by name similarity.

    Lexical only — uses ``difflib.SequenceMatcher`` ratio against the
    surface form so the LLM sees a small relevant menu instead of the
    full entity pool. Future Stage 2.5 will replace this with embedding-
    based similarity once we have entity-level embeddings.
    """
    rows = conn.execute(
        "SELECT id, canonical_name, kind, created_at, created_by, confidence, source_event_id "
        "FROM entities WHERE kind = ?",
        (kind,),
    ).fetchall()
    if not rows:
        return []
    target = surface_form.lower()
    scored: list[tuple[float, Entity]] = []
    for row in rows:
        score = difflib.SequenceMatcher(None, target, row["canonical_name"].lower()).ratio()
        scored.append(
            (
                score,
                Entity(
                    id=row["id"],
                    canonical_name=row["canonical_name"],
                    kind=row["kind"],
                    created_at=row["created_at"],
                    created_by=row["created_by"],
                    confidence=float(row["confidence"]),
                    source_event_id=row["source_event_id"],
                ),
            )
        )
    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:limit]]


def _llm_judge_match(
    *,
    surface_form: str,
    surrounding_text: str,
    candidates: list[Entity],
    model: str,
    api_key: str | None,
) -> _MatchVerdict:
    """One LLM call. Picks one of the candidates or returns matched_entity_id=null."""
    candidate_lines = [
        f"- id: {c.id}\n  name: {c.canonical_name}\n  kind: {c.kind}" for c in candidates
    ]
    user_msg = (
        f"Surface form: {surface_form!r}\n\n"
        f"Surrounding text:\n{surrounding_text}\n\n"
        f"Candidates already in the substrate (kind={candidates[0].kind}):\n"
        + "\n".join(candidate_lines)
        + "\n\nDecide: which candidate (if any) does this surface form refer to?"
    )
    result = call_tool(
        model=model,
        system=_MATCH_SYSTEM_PROMPT,
        user=user_msg,
        tool_name=_MATCH_TOOL_NAME,
        tool_description=_MATCH_TOOL_DESCRIPTION,
        tool_schema=_MATCH_TOOL_SCHEMA,
        api_key=api_key,
        max_tokens=300,
    )
    data = result.data
    matched = data.get("matched_entity_id")
    if matched is not None and not isinstance(matched, str):
        matched = None
    # Defensive: the LLM might hallucinate an ID not in the candidate set.
    valid_ids = {c.id for c in candidates}
    if matched is not None and matched not in valid_ids:
        matched = None
    return _MatchVerdict(
        matched_entity_id=matched,
        reason=str(data.get("reason", "")),
        confidence=float(data.get("confidence", 0.5)),
    )


def _event_surrounding_text(event: Event, extraction: dict[str, Any]) -> str:
    """Compact view of the event used as LLM context for entity judgment.

    Prefers the extractor's summary if available (already distilled);
    falls back to the raw text trimmed to a sensible length.
    """
    summary = extraction.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    text = event.payload.get("text")
    if isinstance(text, str):
        trimmed = text.strip()
        return trimmed[:600] + ("…" if len(trimmed) > 600 else "")
    # observe-event fields
    parts: list[str] = []
    for key in ("action", "subject", "result", "context"):
        v = event.payload.get(key)
        if isinstance(v, str) and v.strip():
            parts.append(f"{key}: {v.strip()}")
    return "\n".join(parts) or "(no text content)"


def _lookup_any_kind(conn: sqlite3.Connection, name: str) -> Entity | None:
    """Find ANY existing entity with this canonical name, prefer highest confidence."""
    hits = find_entity_by_name(conn, canonical_name=name)
    if not hits:
        return None
    return max(hits, key=lambda e: e.confidence)


# ── Phase B: cascade invalidations ────────────────────────────────────────


def _find_uncascaded_invalidations(conn: sqlite3.Connection, max_events: int) -> list[Event]:
    """Invalidate events that haven't been cascaded into entity_edges yet.

    Detected by absence of a CASCADE_PRODUCED_BY interpretation row.
    """
    rows = conn.execute(
        """
        SELECT id, content_hash
        FROM events
        WHERE kind = ?
          AND NOT EXISTS (
              SELECT 1 FROM interpretations i
              WHERE i.event_hash = events.content_hash
                AND i.produced_by = ?
          )
        ORDER BY created_at ASC
        LIMIT ?
        """,
        (INVALIDATE_KIND, CASCADE_PRODUCED_BY, max_events),
    ).fetchall()
    out: list[Event] = []
    for row in rows:
        ev = read_event_by_hash(conn, row["content_hash"])
        if ev is not None:
            out.append(ev)
    return out


def _cascade_invalidation(conn: sqlite3.Connection, invalidate_event: Event) -> int:
    """For each edge sourced from the invalidated target, write an
    edge_invalidations row. Returns the count of edges invalidated.

    ``parent_hashes`` of an invalidate event point at the target's
    content_hash. We look up the target event's id, then find every
    entity_edge sourced from it.
    """
    if not invalidate_event.parent_hashes:
        return 0
    cascaded = 0
    reason = invalidate_event.payload.get("reason") or "cascade from invalidate event"
    for target_hash in invalidate_event.parent_hashes:
        target = read_event_by_hash(conn, target_hash)
        if target is None:
            continue
        edges = find_edges_for_source_event(conn, target.id)
        for edge in edges:
            row = write_edge_invalidation(
                conn,
                edge_id=edge.id,
                invalidated_by=f"event:{invalidate_event.id}",
                reason=str(reason),
                source_event_id=invalidate_event.id,
            )
            if row is not None:
                cascaded += 1
    return cascaded


# ── helpers ───────────────────────────────────────────────────────────────

_VALID_KINDS = {"person", "organization", "place", "project", "product", "concept", "other"}


def _normalize_kind(kind_raw: str) -> str:
    """Map extractor kind strings to the substrate's canonical set.

    The Extractor's tool schema enums to a fixed list (see prompts.py),
    but we accept upstream variants (singular/plural, case) defensively.
    Unknown values fall back to 'other'.
    """
    k = kind_raw.strip().lower()
    if k in _VALID_KINDS:
        return k
    # Common variants.
    if k in {"org", "organisation"}:
        return "organization"
    if k in {"people", "human", "individual"}:
        return "person"
    if k in {"places", "location", "city", "country"}:
        return "place"
    return "other"


def _api_key_for_model(model: str, settings: Settings) -> str | None:
    if model.startswith("anthropic/") and settings.anthropic_api_key is not None:
        return settings.anthropic_api_key.get_secret_value()
    if model.startswith("openai/") and settings.openai_api_key is not None:
        return settings.openai_api_key.get_secret_value()
    if model.startswith("gemini/") and settings.gemini_api_key is not None:
        return settings.gemini_api_key.get_secret_value()
    return None


def _sonnet_for(haiku_model: str) -> str | None:
    """Map a Haiku model identifier to the equivalent Sonnet for escalation.

    Returns None for non-Anthropic models — escalation is currently
    Anthropic-only. The Sonnet model name follows the same versioning
    convention as the Haiku default (see CLAUDE.md for the supported
    Claude 4.X family).
    """
    if not haiku_model.startswith("anthropic/"):
        return None
    if "haiku-4-5" in haiku_model:
        return "anthropic/claude-sonnet-4-6"
    # Unknown Anthropic variant — skip escalation rather than guess.
    return None


def _maybe_sleep(last_llm_call: float | None) -> float:
    """Pace LLM calls. First call doesn't sleep; subsequent ones wait."""
    now = time.monotonic()
    if last_llm_call is not None:
        elapsed = now - last_llm_call
        if elapsed < INTER_CALL_SLEEP_SECONDS:
            time.sleep(INTER_CALL_SLEEP_SECONDS - elapsed)
    return time.monotonic()
