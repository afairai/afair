"""Tool handlers — pure business logic, no FastMCP dependency.

These functions are unit-testable directly without spinning up a server.
The MCP server wrapper in `server.py` registers them with FastMCP and
maps validation errors to MCP error responses.

Three handlers, three verbs:
  - remember(content, *, context, type_hint, parent_hashes, invalidates)
  - recall(query=None, *, scope, depth, limit, by_id, by_content_hash,
           full_payload, stats)
  - observe(event)

Decision history: the surface was collapsed from 6 → 3 tools in
pre-release (2026-05-26, vault decision event 01KSHW6Q0EB1BBPKZ4Q2QT20NT)
to lock in the cleanest forever-API before any external user adopted it.
The old list_context, get_event, and invalidate verbs are absorbed:

  list_context(about=X, limit=N)
    →  recall(query=X, stats=True, limit=N)      (with X)
    →  recall(stats=True, limit=N)               (without X)

  get_event(event_id=X)
    →  recall(by_id=X, full_payload=True)

  get_event(content_hash=X)
    →  recall(by_content_hash=X, full_payload=True)

  invalidate(target_hash=X, reason=Y)
    →  remember(content={"type":"text","text":Y or "(no replacement)"},
                invalidates=[X])
"""

from __future__ import annotations

import base64
import binascii
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..agents import read_latest_interpretation, schedule_extraction
from ..agents.binder import get_linked_event_ids, get_linked_event_ids_batch
from ..agents.conflict_resolver import read_conflicts_batch
from ..agents.embedding import EmbeddingError, embed_query
from ..agents.entity_articles import ENTITY_ARTICLE_KIND
from ..agents.interpretation import (
    read_latest_interpretations_batch,
    read_latest_salience_batch,
)
from ..agents.invalidation import (
    INVALIDATE_KIND,
    InvalidationInfo,
    read_invalidations_batch,
    write_invalidation,
)
from ..agents.verdicts import is_unresolved_conflict
from ..agents.verdicts import meta as _verdict_meta
from ..substrate import (
    build_binary_payload,
    build_blob_ref_payload,
    build_compound_payload,
    build_text_payload,
    count_events_by_client,
    count_pending_corrections,
    count_pending_ontology_proposals,
    iter_events,
    latest_edge_confidence_batch,
    latest_edge_reviews_batch,
    object_exists,
    object_plaintext_size,
    read_edges_by_source_event_ids,
    read_entities_batch,
    read_event_by_hash,
    read_event_by_id,
    read_event_provenance_batch,
    read_mentions_batch,
    read_object,
    read_pending_corrections,
    read_pending_ontology_proposals,
    record_edge_serves,
    record_event_provenance,
    resolve_canonical_batch,
    resolve_entity_kind_batch,
    retracted_entity_ids,
    rrf_merge,
    search_fts,
    search_vec,
    write_event_with_status,
    write_object,
)
from ..substrate import pipeline_events as pe
from ..substrate.belief import (
    _MIN_AUTO_CONFIRM_CONFIDENCE,
    Entrenchment,
    auto_confirm,
    resolve_trust,
)
from ..substrate.corrections import decide_correction
from ..substrate.events import row_to_event
from ..substrate.kinds import ONTOLOGY_PROPOSAL_ID_PREFIX
from ..substrate.search import FTS5_SPECIALS_RE
from ..substrate.temporal import read_event_temporal_batch, temporal_relevance
from . import schemas
from .auth import current_client
from .context import connect_for_thread, get_context
from .schemas import (
    MAX_DECIDE_BATCH,
    MAX_FEEDBACK_IDS_PER_CALL,
    MAX_FEEDBACK_TOPIC_CHARS,
    MAX_PENDING_LIMIT,
    MAX_REMEMBER_BYTES,
    BinaryContent,
    CompoundBlobRefPart,
    CompoundContent,
    CompoundTextPart,
    ConflictFlag,
    ContextSummary,
    CorrectionDecision,
    CorrectionOutcomeView,
    Depth,
    InvalidationSummary,
    ObserveEvent,
    ObserveResult,
    ProposedCorrectionView,
    RecallCoverage,
    RecallFeedback,
    RecallHit,
    RecallResult,
    RecallVerbosity,
    RememberContent,
    RememberResult,
    TextContent,
)

if TYPE_CHECKING:
    from ..substrate.events import Event
    from ..substrate.provenance import ProvenanceRow

# Narrowed error set for a tunable-registry lookup fallback (recall must never
# fail on a tunable hiccup): a whitelist miss (KeyError), a DB hiccup
# (sqlite3.Error), or a malformed stored value (ValueError/TypeError). A real
# programming bug propagates instead of being swallowed.
_TUNABLE_FALLBACK_ERRORS = (KeyError, sqlite3.Error, ValueError, TypeError)

# All MCP-initiated events carry origin "agent" in v1. ``origin`` is part of the
# event content_hash (events.content_hash(kind, origin, payload, parents)), so it
# MUST stay coarse — refining it per-client would fork the dedup/hash contract.
# Which client wrote an event is recorded OUT of the hash in the append-only
# ``event_provenance`` sidecar (ADR-0006), stamped from the authenticated
# credential right after each write below; recall serves it as an additive
# ``RecallHit.client`` / ``ContextSummary.by_client``. That refinement does not
# alter the MCP tool signatures and therefore does not violate I1.
DEFAULT_ORIGIN = "agent"

# Snippet length for text in recall summaries (when full_payload=False).
SUMMARY_TEXT_CHARS = 500

# ── recall verbosity + paging (P1-2) ─────────────────────────────────────────
# Compact is the default shape: the AI-useful minimum per hit. Everything it
# drops is re-fetchable via verbosity="full" or recall(by_id=..., full_payload=True).
COMPACT_TEXT_CHARS = 300  # payload text / observe subject+result / compound part text
COMPACT_CONTEXT_CHARS = 200  # payload context
COMPACT_SUMMARY_CHARS = 280  # interpretation.summary
COMPACT_MAX_ENTITIES = 5  # interpretation.canonical_entities
COMPACT_MAX_EDGES = 5  # interpretation.entity_edges
COMPACT_MAX_LINKED_IDS = 3  # hit.linked_event_ids
COMPACT_MAX_CONFLICTS = 5  # hit.conflicts — bound the otherwise-unbounded pairing vector
COMPACT_CONFLICT_REASON_CHARS = 160
COMPACT_DEFAULT_LIMIT = 10
DEFAULT_RECALL_LIMIT = 20
MAX_RECALL_LIMIT = 100
MAX_RECALL_OFFSET = 200
DEFAULT_PENDING_LIMIT = 20  # served pending-queue page when pending_limit is omitted

# Background pool used to overlap the embedding call with the (cheap)
# local FTS query during recall. Pool is process-level so it survives
# across requests. max_workers=8 covers the realistic multi-tool
# concurrent-recall pattern (Perf audit I2): 4 was too low and queued
# the 5th+ concurrent recall behind an embedding network round-trip.
_RECALL_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="recall-parallel")


class ContentTooLargeError(ValueError):
    """Raised when remember content exceeds MAX_REMEMBER_BYTES."""


class InvalidBase64Error(ValueError):
    """Raised when BinaryContent.data_b64 isn't valid base64."""


def _record_recall_feedback(db: Any, feedback: RecallFeedback) -> None:
    """Persist a RecallFeedback payload as a tuner observation row.

    Best-effort: any failure here logs and continues. Recall must
    never break on a signal-collection error.

    Bounds applied (matching the schema constants):
      - useful_event_ids / not_useful_event_ids capped at
        MAX_FEEDBACK_IDS_PER_CALL each (silent truncate)
      - missing_topic truncated to MAX_FEEDBACK_TOPIC_CHARS
    """
    try:
        useful = list(feedback.useful_event_ids)[:MAX_FEEDBACK_IDS_PER_CALL]
        not_useful = list(feedback.not_useful_event_ids)[:MAX_FEEDBACK_IDS_PER_CALL]
        topic: str | None = None
        if feedback.missing_topic:
            topic = feedback.missing_topic[:MAX_FEEDBACK_TOPIC_CHARS]
        # No-op if every field is empty.
        if not useful and not not_useful and not topic:
            return
        from ..substrate import tuner_state as _ts

        _ts.write(
            db,
            kind="observation",
            worker="recall",
            tunable="feedback",
            evidence={
                "useful_event_ids": useful,
                "not_useful_event_ids": not_useful,
                "missing_topic": topic,
            },
        )
    except Exception as e:
        import structlog as _structlog

        _structlog.get_logger(__name__).warning(
            "recall.feedback_persist_failed",
            error=str(e),
        )


class InvalidRecallArgsError(ValueError):
    """Raised when recall is called with a contradictory mix of selectors
    (e.g., both ``by_id`` and ``by_content_hash`` provided)."""


class InvalidateTargetError(ValueError):
    """Raised when an invalidates target doesn't resolve to an event or
    points at an existing invalidation event (no nested invalidations)."""


# ── helpers ─────────────────────────────────────────────────────────────────


def _payload_summary(
    payload: dict[str, Any],
    *,
    text_cap: int = SUMMARY_TEXT_CHARS,
    context_cap: int | None = None,
) -> dict[str, Any]:
    """Build a truncation-safe view of a payload for recall results.

    ``text_cap`` bounds text bodies (and, in compact, observe subject/result +
    compound part text). ``context_cap`` bounds ``context`` when set (compact
    only; None = verbatim). Binary metadata passes through. Unknown
    content_types still produce a reasonable summary.

    Standard/full pass ``text_cap=SUMMARY_TEXT_CHARS`` and ``context_cap=None``,
    which reproduces the pre-P1-2 output byte-for-byte (observe subject/result
    stay verbatim — the extra clipping only kicks in when ``text_cap`` is below
    the default, i.e. compact)."""
    compact = text_cap < SUMMARY_TEXT_CHARS
    content_type = payload.get("content_type", "unknown")
    summary: dict[str, Any] = {"content_type": content_type}

    if content_type == "text":
        text = payload.get("text", "")
        if isinstance(text, str):
            summary["text"] = text[:text_cap]
    elif content_type == "text-large":
        for k in ("blob_hash", "size_bytes", "mime"):
            if k in payload:
                summary[k] = payload[k]
    elif content_type == "binary":
        for k in ("blob_hash", "size_bytes", "mime", "filename_hint"):
            if k in payload:
                summary[k] = payload[k]
    elif content_type == "event":
        for k in ("action", "subject", "result"):
            if k in payload:
                value = payload[k]
                # action is a short verb; subject/result can be long. Clip the
                # two long fields to text_cap in compact; leave verbatim
                # otherwise (standard/full parity).
                if compact and k in ("subject", "result") and isinstance(value, str):
                    value = value[:text_cap]
                summary[k] = value
    elif content_type == "compound":
        # Compound events surface each part's identity + a truncated
        # text preview. Blob parts stay as references; the caller
        # uses full_payload=True if they need the bytes path.
        parts_summary: list[dict[str, Any]] = []
        for part in payload.get("parts", []):
            if not isinstance(part, dict):
                continue
            part_view: dict[str, Any] = {"type": part.get("type")}
            if part.get("label"):
                part_view["label"] = part["label"]
            if part.get("type") == "text":
                part_text = part.get("text", "")
                if isinstance(part_text, str):
                    part_view["text"] = part_text[:text_cap]
            elif part.get("type") == "blob-ref":
                for k in ("blob_hash", "size_bytes", "mime", "filename_hint"):
                    if k in part:
                        part_view[k] = part[k]
            parts_summary.append(part_view)
        summary["parts"] = parts_summary

    # Common metadata across content types
    for k in ("context", "type_hint", "language", "target_hash", "reason"):
        if payload.get(k) is not None:
            value = payload[k]
            if k == "context" and context_cap is not None and isinstance(value, str):
                value = value[:context_cap]
            summary[k] = value

    return summary


def _materialize_full_payload(payload: dict[str, Any], vault_dir: Any) -> dict[str, Any]:
    """Return a payload with ``text-large`` blobs read back into ``text``.

    The substrate stores large text in the object store referenced by
    ``blob_hash``. For ``full_payload=True`` calls we surface the full
    text inline so callers see one consistent shape. Binary payloads
    are left as-is — the raw bytes stay in the object store; the
    payload metadata (mime, size, filename_hint, blob_hash) is enough
    for most callers, and a dedicated blob-fetch capability is left
    for later phases.
    """
    content_type = payload.get("content_type")
    if content_type != "text-large":
        return dict(payload)

    blob_hash = payload.get("blob_hash")
    if not isinstance(blob_hash, str):
        return dict(payload)

    materialized = dict(payload)
    try:
        raw = read_object(vault_dir, blob_hash)
        materialized["text"] = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        materialized["text_unavailable"] = (
            f"failed to read object {blob_hash!s}: {type(e).__name__}: {e}"
        )
    return materialized


def _text_was_truncated(payload: dict[str, Any], cap: int = SUMMARY_TEXT_CHARS) -> bool:
    """True iff the payload-summary form would clip the text at ``cap`` (so the
    ``truncated`` flag stays honest per verbosity level)."""
    if payload.get("content_type") != "text":
        return False
    text = payload.get("text", "")
    return isinstance(text, str) and len(text) > cap


_INTERPRETATION_SURFACE_KEYS = (
    "best_guess_kind",
    "summary",
    "entities",
    "salient_facts",
    "language",
    "confidence",
    "source_attribution",
)


def _interpretation_summary(extraction: dict[str, Any]) -> dict[str, Any] | None:
    """Pick the AI-useful subset of an Extractor's output for inclusion in
    a recall hit. Returns None when no useful fields are present.

    Uses the truthiness shortcut ``if extraction.get(key)`` — None/""/[]/{}
    are all falsy, so a single check covers all four sentinel values
    without the per-iteration tuple allocation the previous code had
    (Perf audit minor).
    """
    surface: dict[str, Any] = {}
    for key in _INTERPRETATION_SURFACE_KEYS:
        value = extraction.get(key)
        if value:
            surface[key] = value
    return surface or None


def _strip_null_validity(edge: dict[str, Any]) -> dict[str, Any]:
    """Drop ``valid_from``/``valid_to`` from an edge view when they are null
    (they are non-null only for genuinely time-bounded relations, a small
    minority — omitting the nulls is pure wire savings, no information loss)."""
    return {k: v for k, v in edge.items() if not (k in ("valid_from", "valid_to") and v is None)}


def _render_why_durable(
    salience_extraction: dict[str, Any], overlay: dict[str, Any] | None
) -> str | None:
    """A compact human-readable "why this memory is durable" line (W2).

    Pure: composes the salience score, its top-2 nonzero component drivers, and
    the temporal_class + surprise_score already on the overlay into one string.
    Every part is omitted when absent; returns None when nothing is available
    (an event with no salience row AND no temporal/surprise signal). Never
    raises — a malformed component value is simply skipped.
    """
    parts: list[str] = []
    sal = salience_extraction.get("salience")
    components = salience_extraction.get("salience_components")
    top: list[str] = []
    if isinstance(components, dict):
        drivers = sorted(
            (
                (k, v)
                for k, v in components.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
            ),
            key=lambda kv: kv[1],
            reverse=True,
        )
        top = [k for k, _ in drivers[:2]]
    if isinstance(sal, (int, float)) and not isinstance(sal, bool):
        line = f"salience {round(float(sal), 2)}"
        if top:
            line += f" ({', '.join(top)})"
        parts.append(line)
    tclass = overlay.get("temporal_class") if overlay else None
    if tclass:
        parts.append(f"temporal:{tclass}")
    surprise = overlay.get("surprise_score") if overlay else None
    if isinstance(surprise, (int, float)) and not isinstance(surprise, bool):
        parts.append(f"surprise {round(float(surprise), 2)}")
    return "; ".join(parts) if parts else None


def _shape_interpretation(
    extraction: dict[str, Any] | None,
    overlay: dict[str, Any] | None,
    verbosity: str,
    salience_extraction: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build the per-hit ``interpretation`` dict at the requested verbosity.

    - ``full``: today's shape verbatim — the extractor surface subset plus the
      entity/temporal overlay merged in (byte-parity with the pre-P1-2 builder).
    - ``standard``: full, minus the raw ``entities`` list when canonical
      entities are present (redundant) and minus null edge validity bounds.
    - ``compact``: the AI-useful minimum — best_guess_kind + capped summary,
      canonical entities trimmed to ``{id, canonical_name, kind}`` (≤N), edges
      trimmed (≤N, null validity dropped), surprise_score, temporal_class. All
      other extractor/overlay fields are dropped (re-fetchable via full/by_id).
    """
    if verbosity != "compact":
        base = _interpretation_summary(extraction) if extraction is not None else None
        if overlay:
            base = base if base is not None else {}
            base.update(overlay)
        # Durability rationale (W2) — full verbosity ONLY, so compact/standard
        # stay on their existing shape and add zero queries. Merged here (not a
        # new RecallHit field) so the surface freeze holds; keys are simply
        # absent when there is no salience row.
        if verbosity == "full" and salience_extraction is not None:
            durability: dict[str, Any] = {}
            sal = salience_extraction.get("salience")
            if isinstance(sal, (int, float)) and not isinstance(sal, bool):
                durability["salience"] = round(float(sal), 3)
            components = salience_extraction.get("salience_components")
            if components is not None:
                durability["salience_components"] = components
            why = _render_why_durable(salience_extraction, overlay)
            if why is not None:
                durability["why_durable"] = why
            if durability:
                base = base if base is not None else {}
                base.update(durability)
        if base is None:
            return None
        if verbosity == "standard":
            # (1) raw entities are redundant once canonical entities exist.
            if "canonical_entities" in base and "entities" in base:
                base = {k: v for k, v in base.items() if k != "entities"}
            # (2) omit null edge validity bounds.
            edges = base.get("entity_edges")
            if edges:
                base = dict(base)
                base["entity_edges"] = [_strip_null_validity(e) for e in edges]
        return base or None

    # compact
    interp: dict[str, Any] = {}
    if extraction:
        bgk = extraction.get("best_guess_kind")
        if bgk:
            interp["best_guess_kind"] = bgk
        summary = extraction.get("summary")
        if summary:
            interp["summary"] = (
                summary[:COMPACT_SUMMARY_CHARS] if isinstance(summary, str) else summary
            )
    if overlay:
        canonical = overlay.get("canonical_entities")
        if canonical:
            interp["canonical_entities"] = [
                {"id": e["id"], "canonical_name": e["canonical_name"], "kind": e["kind"]}
                for e in canonical[:COMPACT_MAX_ENTITIES]
            ]
        edges = overlay.get("entity_edges")
        if edges:
            interp["entity_edges"] = [_strip_null_validity(e) for e in edges[:COMPACT_MAX_EDGES]]
        # surprise_score may be 0.0 (all-familiar) — keep it explicitly.
        if "surprise_score" in overlay:
            interp["surprise_score"] = overlay["surprise_score"]
        if "temporal_class" in overlay:
            interp["temporal_class"] = overlay["temporal_class"]
    return interp or None


def _invalidation_to_summary(info: InvalidationInfo | None) -> InvalidationSummary | None:
    if info is None:
        return None
    return InvalidationSummary(at=info.at, by_event_id=info.by_event_id, reason=info.reason)


def _earliest_client(rows: list[ProvenanceRow] | None) -> str | None:
    """The author client for an event (ADR-0006): the earliest-stamped row's
    client. ``read_event_provenance_batch`` returns rows ordered by stamped_at
    ASC, so the first is the author. None when the event has no provenance."""
    return rows[0].client if rows else None


def _event_to_hit(
    event: Event,
    db: Any,
    *,
    full_payload: bool,
    invalidation: InvalidationInfo | None = None,
    conflicts: list[dict[str, Any]] | None = None,
    entity_overlay: dict[str, Any] | None = None,
    interpretation_extraction: dict[str, Any] | None = None,
    salience_extraction: dict[str, Any] | None = None,
    linked_event_ids: list[str] | None = None,
    client: str | None = None,
    verbosity: str = "standard",
) -> RecallHit:
    """Build one RecallHit. When ``full_payload`` is True the payload is
    materialized in full (text-large blobs read back from the object
    store); when False, the payload is the truncated summary view.

    ``verbosity`` shapes the interpretation/conflicts/linked-list detail
    (``compact`` | ``standard`` | ``full`` — see ``_shape_interpretation``). It
    is orthogonal to ``full_payload`` (payload materialization). ``standard``
    (the default here) preserves the pre-P1-2 shape apart from two harmless
    subtractions; single-event lookups pass ``full``.

    All per-hit DB lookups (interpretation, bind, invalidation, conflict,
    entity overlay) MUST be batched at the call site and passed in as
    kwargs — this function does no I/O of its own. The batched-version
    closed Perf audit C3 (was N+1 with limit=20 → 40+ queries; now 2).

    ``entity_overlay`` carries the Phase 4 Track 1 enrichment —
    pre-computed ``canonical_entities`` + ``entity_edges`` for this
    event's content_hash. The recall handler batch-fetches these once
    per call and threads the per-event slice in here."""
    compact = verbosity == "compact"
    if full_payload:
        ctx = get_context()
        payload_view = _materialize_full_payload(event.payload, ctx.vault_dir)
        truncated = False
    else:
        text_cap = COMPACT_TEXT_CHARS if compact else SUMMARY_TEXT_CHARS
        context_cap = COMPACT_CONTEXT_CHARS if compact else None
        payload_view = _payload_summary(event.payload, text_cap=text_cap, context_cap=context_cap)
        truncated = _text_was_truncated(event.payload, text_cap)

    # interpretation_extraction is the raw dict from the batch helper.
    # When unset (legacy callers that didn't migrate), fall back to the
    # per-hit query — keeps the function backward-compatible for any
    # test that hasn't been updated.
    if interpretation_extraction is None:
        interp = read_latest_interpretation(db, event.content_hash)
        interpretation_extraction = interp.extraction if interp is not None else None

    # Shape the interpretation dict at the requested verbosity. The overlay
    # (canonical entities + edges + surprise + temporal) is merged inside the
    # shaper so no RecallHit field is added (the 2026-05-26 surface freeze holds;
    # only the free-form dict content varies).
    interpretation = _shape_interpretation(
        interpretation_extraction, entity_overlay, verbosity, salience_extraction
    )

    # Same fall-back for linked_event_ids when the caller didn't batch.
    if linked_event_ids is None:
        linked_event_ids = get_linked_event_ids(db, event.content_hash)
    if compact and linked_event_ids:
        linked_event_ids = linked_event_ids[:COMPACT_MAX_LINKED_IDS]

    if compact:
        # Keep only conflicts that carry a user-facing signal (unresolved OR a
        # caveat template); drop the caveat-less confirms/compatible/unrelated/
        # evolves. Cap the count (an event can be in tension with many others —
        # an unbounded vector otherwise) AND each reason. The FULL conflict
        # history is one verbosity="full"/by_id away, and coverage's unresolved
        # count is computed from the UNFILTERED map elsewhere, so the honesty
        # signal is never capped — only the per-hit list is.
        kept: list[dict[str, Any]] = []
        for c in conflicts or []:
            verdict = str(c.get("verdict", ""))
            if is_unresolved_conflict(verdict) or _verdict_meta(verdict).caveat is not None:
                c2 = dict(c)
                reason = c2.get("reason")
                if isinstance(reason, str):
                    c2["reason"] = reason[:COMPACT_CONFLICT_REASON_CHARS]
                kept.append(c2)
        conflict_flags: list[ConflictFlag] = [
            ConflictFlag(**c) for c in kept[:COMPACT_MAX_CONFLICTS]
        ]
    else:
        conflict_flags = [ConflictFlag(**c) for c in (conflicts or [])]
    return RecallHit(
        event_id=event.id,
        content_hash=event.content_hash,
        created_at=event.created_at,
        kind=event.kind,
        origin=event.origin,
        payload=payload_view,
        truncated=truncated,
        interpretation=interpretation,
        linked_event_ids=linked_event_ids,
        parent_hashes=list(event.parent_hashes or []),
        invalidation=_invalidation_to_summary(invalidation),
        conflicts=conflict_flags,
        # Provenance (ADR-0006) is served standard/full/by_id; compact omits it
        # (null is dropped from the wire by the exclude_none serializer).
        client=None if compact else client,
    )


# Per-thread cache for _recent_canonical_context. The recall hot path
# computes this on every call; the underlying data only changes when a
# new remember/observe lands. Caching by "latest event id" gives perfect
# invalidation: as long as the substrate's most recent event id hasn't
# moved, the cached set is correct (Perf audit minor — recall canonical
# context).
#
# Per-thread (threading.local) rather than module-level locked dict so
# we don't serialize concurrent recalls behind a single lock. Each
# uvicorn worker pays the build cost once between writes — typically
# 1-3 ms saved on back-to-back recalls (the Claude-Code thinking loop
# pattern).
_canonical_context_cache = threading.local()


def _recent_canonical_context(db: Any, window: int) -> set[str]:
    """Set of canonical entity IDs mentioned in the last ``window`` events.

    Used by the Phase 4 Track 2 surprise score: each recall hit is
    measured against this set. Hits whose canonical entities all live
    in this set score 0.0 (familiar); hits with no overlap score 1.0
    (novel / "where did this come from").

    Cheap to compute — one indexed-join + a merge-resolve pass over the
    deduped entity IDs. Window covers ``remember``+``observe`` events
    (the user-driven kinds); ignores ``consolidation``/``invalidate``
    which are system-generated.

    Cached per-thread keyed on (window, latest_event_id). A single
    indexed lookup for the latest event id decides cache validity; if
    it hasn't changed since the last call, we return the cached set.
    """
    if window <= 0:
        return set()

    latest_row = db.execute("SELECT id FROM events ORDER BY created_at DESC LIMIT 1").fetchone()
    latest_id = latest_row["id"] if latest_row else None

    cached_key = getattr(_canonical_context_cache, "key", None)
    cached_value: set[str] | None = getattr(_canonical_context_cache, "value", None)
    new_key = (window, latest_id)
    if cached_key == new_key and cached_value is not None:
        return cached_value

    rows = db.execute(
        """
        SELECT DISTINCT em.entity_id
        FROM entity_mentions em
        JOIN events e ON e.id = em.event_id
        WHERE e.kind IN ('remember', 'observe')
          AND e.id IN (
              SELECT id FROM events
              WHERE kind IN ('remember', 'observe')
              ORDER BY created_at DESC
              LIMIT ?
          )
        """,
        (window,),
    ).fetchall()
    raw_ids = [r["entity_id"] for r in rows]
    if not raw_ids:
        result: set[str] = set()
    else:
        resolved = resolve_canonical_batch(db, raw_ids)
        result = set(resolved.values())

    _canonical_context_cache.key = new_key
    _canonical_context_cache.value = result
    return result


def _compute_surprise_score(
    hit_canonical_ids: list[str], recent_context: set[str]
) -> tuple[float, int, int] | None:
    """Entity-novelty surprise. Returns (score, novel_count, total_count)
    or None when the hit has no canonical entities to score against.

    score ∈ [0, 1]: 0 = all entities familiar, 1 = all entities novel.
    """
    if not hit_canonical_ids:
        return None
    unique = set(hit_canonical_ids)
    novel = unique - recent_context
    return (len(novel) / len(unique), len(novel), len(unique))


def _resolve_auto_confirm_floor(db: Any) -> float:
    """Resolve the auto-confirm confidence floor through the tuner registry,
    falling back to the belief-module default (surprise-window pattern, same as
    the surprise-context-window lookup above).

    Until S8 registers ``belief.auto_confirm_floor``, ``registry.get`` raises
    KeyError and the except path serves the static default — recall must NEVER
    fail because a tunable lookup misbehaved."""
    from ..agents.tunable_registry import (
        TunableRegistry as _TunableRegistry,  # local import to avoid cycle
    )

    try:
        registry = _TunableRegistry(db)
        return float(registry.get("belief", "auto_confirm_floor"))
    except _TUNABLE_FALLBACK_ERRORS as exc:
        import structlog as _structlog

        _structlog.get_logger(__name__).warning(
            "tunable_registry.fallback",
            worker="recall",
            tunable="belief.auto_confirm_floor",
            error=str(exc),
        )
        return _MIN_AUTO_CONFIRM_CONFIDENCE


def _build_entity_overlay(events: list[Event], db: Any) -> dict[str, dict[str, Any]]:
    """For every event in ``events``, compute the per-hit entity overlay.

    Three batched reads (one per logical concern):
      1. mentions per event_hash
      2. canonical-entity rows for every mentioned entity_id, resolved
         through the merge chain so superseded entities surface as their
         current canonical (decision #6)
      3. edges sourced from every event_id, filtered to non-invalidated
         (decision #6 again — hide superseded by default)

    Plus one Phase-4-Track-2 read (the recent-context window for the
    per-hit surprise score) that's amortized across all hits.

    Returns ``{content_hash → {canonical_entities, entity_edges, surprise_score, surprise_components}}``.
    Events with neither mentions nor edges are absent from the result;
    the caller treats that as "no overlay" and leaves interpretation as-is.
    """
    if not events:
        return {}

    event_hashes = [e.content_hash for e in events]
    event_ids = [e.id for e in events]

    mentions_by_hash = read_mentions_batch(db, event_hashes)
    edges_by_event_id = read_edges_by_source_event_ids(db, event_ids)

    if not mentions_by_hash and not edges_by_event_id:
        return {}

    # Gather every raw entity_id referenced, then resolve through merges
    # and bulk-fetch the resolved canonicals in one query.
    raw_ids: set[str] = set()
    for mentions in mentions_by_hash.values():
        for m in mentions:
            raw_ids.add(m.entity_id)
    for edges in edges_by_event_id.values():
        for e in edges:
            raw_ids.add(e.subject_id)
            raw_ids.add(e.object_id)

    resolved_map = resolve_canonical_batch(db, list(raw_ids))
    canonical_entities = read_entities_batch(db, resolved_map.values())
    # ADR-0003 Phase 2: serve each entity's CURRENT kind (latest assignment,
    # else its immutable stored kind, piped through the registry chain) — a
    # retype is one assignment row and recall reflects it immediately.
    kind_by_entity = resolve_entity_kind_batch(db, list(canonical_entities.keys()))
    # Retracted (noise) entities are withdrawn from the live graph — never
    # surface them, nor edges that touch them, even though their rows + mentions
    # remain as history (I2).
    retracted = retracted_entity_ids(db)

    # Phase 4 Track 2 — recent context for the per-hit surprise score.
    # Computed once per recall and applied to every hit.
    #
    # As of 2026-06-03 the window size resolves through three layers:
    #   1. Tuner-promoted value in the registry (registry != default)
    #      → trust the tuner.
    #   2. Operator override via Settings/env → ctx wins.
    #   3. Static default → both agree, doesn't matter which.
    #
    # The try/except fallback protects against registry hiccup —
    # recall must NEVER fail because tunable lookup misbehaved.
    ctx = get_context()
    from ..agents.tunable_registry import (
        TunableRegistry as _TunableRegistry,  # local import to avoid cycle
    )

    try:
        _registry = _TunableRegistry(db)
        _spec = _registry.get_spec("surprise", "context_window")
        _reg_value = _registry.get("surprise", "context_window")
        surprise_window = _reg_value if _reg_value != _spec.default else ctx.surprise_context_window
    except _TUNABLE_FALLBACK_ERRORS as exc:
        import structlog as _structlog

        _structlog.get_logger(__name__).warning(
            "tunable_registry.fallback",
            worker="recall",
            tunable="surprise.context_window",
            error=str(exc),
        )
        surprise_window = ctx.surprise_context_window
    recent_context = _recent_canonical_context(db, surprise_window)

    overlay: dict[str, dict[str, Any]] = {}
    for content_hash, mentions in mentions_by_hash.items():
        # Dedupe canonical entities — multiple surface forms can map to
        # one canonical (e.g., "Sajinth" + "Saji" both → same entity).
        seen: set[str] = set()
        ents: list[dict[str, Any]] = []
        for m in mentions:
            canonical_id = resolved_map.get(m.entity_id, m.entity_id)
            if canonical_id in seen or canonical_id in retracted:
                continue
            seen.add(canonical_id)
            entity = canonical_entities.get(canonical_id)
            if entity is None:
                continue
            ents.append(
                {
                    "id": entity.id,
                    "canonical_name": entity.canonical_name,
                    "kind": kind_by_entity.get(entity.id, entity.kind),
                    "surface_form": m.surface_form,
                    "match_method": m.match_method,
                }
            )
        if ents:
            overlay.setdefault(content_hash, {})["canonical_entities"] = ents
            # Phase 4 Track 2 — surface the per-hit surprise score.
            # Computed from the *resolved* canonical IDs (post-merge), so
            # supersession is honored in the comparison: an entity that's
            # been merged into another's canonical inherits its
            # familiar-or-novel status.
            hit_canonical_ids = [e["id"] for e in ents]
            surprise = _compute_surprise_score(hit_canonical_ids, recent_context)
            if surprise is not None:
                score, novel, total = surprise
                overlay[content_hash]["surprise_score"] = round(score, 3)
                overlay[content_hash]["surprise_components"] = {
                    "novel_entity_count": novel,
                    "total_entity_count": total,
                    "window_size": surprise_window,
                }

    # Attach edges keyed by content_hash via event_id reverse-map. Each edge
    # is a defeasible belief (ADR-0002): mark its trust state so recall never
    # serves a `proposed` edge as hard fact. Invalidated/rejected edges are
    # already filtered out upstream, so these resolve to confirmed /
    # auto_confirmed / proposed. Reviews fetched in one batched query.
    all_edge_ids = [edge.id for edges in edges_by_event_id.values() for edge in edges]
    review_verdicts = latest_edge_reviews_batch(db, all_edge_ids)
    # SERVED confidence (ADR-0004): the latest score row per edge, falling back
    # to the immutable at-discovery column when no score exists yet (old vaults,
    # mid-backfill). This is the number the auto-confirm gate now judges — the
    # frozen write-time column would make the gate vacuous.
    served_confidence = latest_edge_confidence_batch(db, all_edge_ids)
    auto_confirm_floor = _resolve_auto_confirm_floor(db)
    event_id_to_hash = {e.id: e.content_hash for e in events}
    # Edges that actually reach a recall response (ADR — serve-gated review):
    # only these earn a review-queue slot, and the auto-expiry sweep keys on
    # the ABSENCE of a serve stamp. Collected across all hits, stamped once
    # below in a single fail-soft write.
    served_edge_ids: list[str] = []
    for source_event_id, edges in edges_by_event_id.items():
        edge_content_hash = event_id_to_hash.get(source_event_id)
        if edge_content_hash is None:
            continue
        edge_views: list[dict[str, Any]] = []
        for edge in edges:
            subj_id = resolved_map.get(edge.subject_id, edge.subject_id)
            obj_id = resolved_map.get(edge.object_id, edge.object_id)
            if subj_id in retracted or obj_id in retracted:
                continue
            subj_canonical = canonical_entities.get(subj_id)
            obj_canonical = canonical_entities.get(obj_id)
            if subj_canonical is None or obj_canonical is None:
                continue
            # has_evidence=True: post-evidence-gate, every edge that exists was
            # grounded in a verbatim quote. source_entrenchment defaults to
            # AGENT_DERIVED; foreign-import downgrading is a later slice.
            conf = served_confidence.get(edge.id, edge.confidence)
            trust = resolve_trust(
                latest_verdict=review_verdicts.get(edge.id),
                is_invalidated=False,
                auto_confirmed=auto_confirm(
                    confidence=conf,
                    predicate=edge.predicate,
                    source_entrenchment=Entrenchment.AGENT_DERIVED,
                    floor=auto_confirm_floor,
                ),
            )
            edge_views.append(
                {
                    "subject": subj_canonical.canonical_name,
                    "predicate": edge.predicate,
                    "object": obj_canonical.canonical_name,
                    "valid_from": edge.valid_from,
                    "valid_to": edge.valid_to,
                    "trust": trust.value,
                    "confidence": round(conf, 3),
                }
            )
            served_edge_ids.append(edge.id)
        if edge_views:
            overlay.setdefault(edge_content_hash, {})["entity_edges"] = edge_views

    # Stamp the served edges (ADR — serve-gated review). Fail-soft: a stamp
    # failure must never fail or meaningfully slow a recall; the review gate
    # simply misses this edge for one cycle and the next recall re-stamps it.
    if served_edge_ids:
        try:
            record_edge_serves(db, served_edge_ids)
        except Exception:
            import structlog as _structlog

            _structlog.get_logger(__name__).warning("recall.edge_serve_record_failed")

    return overlay


def _attach_invalidations(events: list[Event], db: Any) -> dict[str, InvalidationInfo]:
    """Batch-fetch invalidation info for a list of events."""
    if not events:
        return {}
    hashes = [e.content_hash for e in events]
    return read_invalidations_batch(db, hashes)


def _attach_conflicts(events: list[Event], db: Any) -> dict[str, list[dict[str, Any]]]:
    """Batch-fetch conflict-resolver verdicts for a list of events."""
    if not events:
        return {}
    hashes = [e.content_hash for e in events]
    return read_conflicts_batch(db, hashes)


_ENTITY_MATCH_MAX_TOKENS = 2
_ENTITY_MATCH_MAX_TOKEN_LEN = 30


def _events_via_entity_match(db: Any, query: str, *, limit: int) -> list[Event]:
    """Find events whose canonical entities or surface forms match the query.

    Phase 4 Track 1 Stage 4 — entity-aware routing. Auto-detects when a
    query is "really" an entity reference by checking both canonical
    names AND historical surface forms (so ``recall(query="Saji")``
    still finds events even though the canonical is "Sajinth").

    Returns most-recent-first events, capped at ``limit``. Empty result
    when nothing matches — caller treats this as "no entity boost" and
    falls back to plain FTS+vec.

    Perf audit C5 / P2a: the expression indexes DO exist now
    (``entity_mentions_surface_lower_idx``, ``entities_canonical_lower_idx``),
    but a single ``OR`` across the two tables through a LEFT JOIN cannot use
    either — SQLite's OR-optimization only fires within one table, so it
    scanned ``entity_mentions`` and probed ``entities`` per row. The query is
    rewritten as a UNION of two independently-indexed lookups: the first arm
    drives from ``entity_mentions_surface_lower_idx``, the second from
    ``entities_canonical_lower_idx`` → ``entity_mentions_entity_idx``. UNION
    also dedupes, so the old ``DISTINCT`` is gone. For multi-token / very-long
    queries — typical of prose questions, never entity references — we still
    bail out immediately.
    """
    stripped = query.strip()
    if not stripped:
        return []
    tokens = stripped.split()
    if len(tokens) > _ENTITY_MATCH_MAX_TOKENS:
        return []
    if any(len(t) > _ENTITY_MATCH_MAX_TOKEN_LEN for t in tokens):
        return []
    # UNION of two indexed arms, folded into a subquery so the id set never
    # materializes into a Python-built ``IN (?, ?, ...)`` list — that removes
    # the host-parameter ceiling (an entity mentioned in >32,766 events, the
    # operator's own name, used to crash recall with ``too many SQL variables``).
    rows = db.execute(
        """
        SELECT * FROM events
        WHERE id IN (
            SELECT em.event_id FROM entity_mentions em
            WHERE LOWER(em.surface_form) = LOWER(:name)
            UNION
            SELECT em.event_id FROM entity_mentions em
            JOIN entities ent ON ent.id = em.entity_id
            WHERE LOWER(ent.canonical_name) = LOWER(:name)
        )
        ORDER BY created_at DESC
        LIMIT :limit
        """,
        {"name": stripped, "limit": limit},
    ).fetchall()
    return [row_to_event(r) for r in rows]


def _api_key_for_embedding(ctx: Any) -> str | None:
    """Return the right API key for the configured embedding model."""
    model = ctx.embedding_model
    key = None
    if model.startswith("openai/"):
        key = ctx.openai_api_key
    elif model.startswith("voyage/"):
        key = ctx.voyage_api_key
    elif model.startswith("gemini/"):
        key = ctx.gemini_api_key
    elif model.startswith("anthropic/"):
        key = ctx.anthropic_api_key
    else:
        # Unknown provider — try the OpenAI key as least-bad default.
        key = ctx.openai_api_key
    return key.get_secret_value() if key is not None else None


# ── remember ────────────────────────────────────────────────────────────────


def _truncate_preserve(value: str | None, cap: int) -> tuple[str | None, str | None]:
    """Truncate an over-long ``remember`` field to ``cap``, returning
    ``(truncated, full_original_or_None)``.

    Mirrors ``ObserveEvent._truncate_long_fields``: the field is never rejected
    for length (write-first intake); when it exceeds the cap the full original
    is returned separately so the caller can preserve it under ``<field>_full``
    in the payload. Returns the value unchanged and ``None`` when within cap.
    """
    if value is not None and len(value) > cap:
        return value[:cap], value
    return value, None


def _stamp_provenance(db: Any, event_id: str, verb: str) -> None:
    """Stamp the authenticated client for a just-written event (ADR-0006).

    Runs REGARDLESS of whether the write was a fresh insert or a dedup: a second
    client writing the same content-hashed event appends its own honest row
    (``INSERT OR IGNORE`` on ``UNIQUE(event_id, client)`` no-ops a same-client
    re-stamp). ``current_client()`` is None outside an HTTP request (direct/
    in-process calls) — then nothing is stamped. Fail-soft: a provenance failure
    must never fail or meaningfully slow the underlying remember/observe.
    """
    ident = current_client()
    if ident is None:
        return
    try:
        record_event_provenance(
            db, event_id=event_id, client=ident[0], auth_kind=ident[1], verb=verb
        )
    except Exception:
        import structlog as _structlog

        _structlog.get_logger(__name__).warning("provenance.stamp_failed", event_id=event_id)


def remember(
    content: RememberContent,
    context: str | None = None,
    type_hint: str | None = None,
    parent_hashes: list[str] | None = None,
    invalidates: list[str] | None = None,
) -> RememberResult:
    """Write a fact to the substrate, optionally invalidating prior facts.

    The ``invalidates`` kwarg supersedes prior facts in the same call
    (bi-temporal correction). Each target hash gets its own invalidation
    event with ``kind='invalidate'`` and ``parent_hashes=[target]`` for
    lineage. The new ``content`` is written first; invalidations follow.
    """
    # Bound the never-documented-unbounded kwargs at the handler entry.
    # The MCP signature is the v1 surface (I1, frozen) so we tighten by
    # validating the inputs server-side rather than changing the public
    # signature. These limits are generous for any legitimate caller and
    # cheap protection against floods that would explode the FTS index,
    # exhaust the validator, or serialize a million invalidation
    # transactions inside a single request handler.
    # Over-long ``context`` / ``type_hint`` are truncated to their caps rather
    # than rejected — write-first intake, mirroring ObserveEvent's field
    # handling. The full original is preserved under ``<field>_full`` in the
    # payload (injected below) so nothing the caller sent is lost (I2 spirit);
    # a client that hands in a 5KB context persists rather than being dropped
    # at the handler boundary.
    context, context_full = _truncate_preserve(context, schemas.MAX_CONTEXT_CHARS)
    type_hint, type_hint_full = _truncate_preserve(type_hint, schemas.MAX_TYPE_HINT_CHARS)
    if parent_hashes is not None and len(parent_hashes) > schemas.MAX_PARENT_HASHES_PER_CALL:
        msg = (
            f"parent_hashes must be <= {schemas.MAX_PARENT_HASHES_PER_CALL} entries; "
            f"got {len(parent_hashes)}"
        )
        raise ValueError(msg)
    if invalidates is not None and len(invalidates) > schemas.MAX_INVALIDATES_PER_CALL:
        msg = (
            f"invalidates must be <= {schemas.MAX_INVALIDATES_PER_CALL} entries; "
            f"got {len(invalidates)}"
        )
        raise ValueError(msg)

    ctx = get_context()
    db = connect_for_thread()

    # Validate the invalidation targets BEFORE writing any content, so a bad
    # target rejects the whole call atomically. Previously this ran AFTER the
    # content write, so a malformed hash in ``invalidates`` left the content
    # event (and any earlier invalidations) already committed while the call
    # still raised — a partial write the caller couldn't see. This pass is
    # read-only (I2: no substrate mutation); the actual invalidation writes
    # still happen after the content event so lineage references a real id.
    for target_hash in invalidates or []:
        target = read_event_by_hash(db, target_hash)
        if target is None:
            msg = f"invalidates target not found: {target_hash!r}"
            raise InvalidateTargetError(msg)
        if target.kind == INVALIDATE_KIND:
            msg = (
                f"invalidates target {target_hash!r} is itself an "
                "invalidation event; nested invalidations are not supported"
            )
            raise InvalidateTargetError(msg)

    # When text spills to the object store (text-large), the canonical payload
    # holds only a blob_hash, so the FTS index would miss the body. Carry the
    # full text as the searchable body so a large paste stays findable by its
    # contents from the moment it's written (the Extractor enriches further,
    # but the keyword index must not wait on the cold path). None for every
    # other content shape — the body is already in the payload or is binary.
    searchable_body: str | None = None

    if isinstance(content, TextContent):
        encoded = content.text.encode("utf-8")
        if len(encoded) > MAX_REMEMBER_BYTES:
            msg = f"text content is {len(encoded)} bytes; max allowed in v1 is {MAX_REMEMBER_BYTES}"
            raise ContentTooLargeError(msg)
        payload = build_text_payload(
            text=content.text,
            context=context,
            type_hint=type_hint,
            vault_dir=ctx.vault_dir,
            inline_text_max_bytes=ctx.inline_text_max_bytes,
        )
        if payload.get("content_type") == "text-large":
            searchable_body = content.text
    elif isinstance(content, BinaryContent):
        try:
            raw = base64.b64decode(content.data_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            msg = "data_b64 is not valid base64"
            raise InvalidBase64Error(msg) from e
        if len(raw) > MAX_REMEMBER_BYTES:
            msg = f"binary content is {len(raw)} bytes; max allowed in v1 is {MAX_REMEMBER_BYTES}"
            raise ContentTooLargeError(msg)
        payload = build_binary_payload(
            data=raw,
            mime=content.mime,
            filename_hint=content.filename_hint,
            context=context,
            type_hint=type_hint,
            vault_dir=ctx.vault_dir,
        )
    elif isinstance(content, CompoundContent):
        # Materialize each part into its on-disk representation. Small text
        # parts pass through inline; a text part over the inline threshold
        # spills to the object store (a ``text-large`` part carrying only a
        # blob_hash) so a multi-MB compound doesn't write a multi-MB SQLite
        # row. Blob-ref parts validate against the object store and inflate
        # with size_bytes — same shape as the single-payload BlobRefContent
        # path so the extractor and recall can treat parts uniformly.
        #
        # A sum-of-parts byte cap mirrors the single-text/binary paths'
        # MAX_REMEMBER_BYTES v1 lock (the 12 MB body middleware is the first
        # gate; this is the second, applied to the decoded text total).
        materialized_parts: list[dict[str, Any]] = []
        # Text of parts that spilled to blobs — routed into searchable_body so
        # FTS still covers them (derive_searchable_text only walks INLINE part
        # text; a text-large part carries no ``text`` key). Inline parts are
        # covered by the payload walk, so only spilled text needs carrying.
        compound_searchable: list[str] = []
        total_text_bytes = 0
        for idx, part in enumerate(content.parts):
            if isinstance(part, CompoundTextPart):
                encoded_part = part.text.encode("utf-8")
                total_text_bytes += len(encoded_part)
                if total_text_bytes > MAX_REMEMBER_BYTES:
                    msg = (
                        f"compound text parts total {total_text_bytes} bytes; "
                        f"max allowed in v1 is {MAX_REMEMBER_BYTES}"
                    )
                    raise ContentTooLargeError(msg)
                if len(encoded_part) > ctx.inline_text_max_bytes:
                    part_dict = {
                        "type": "text-large",
                        "blob_hash": write_object(ctx.vault_dir, encoded_part),
                        "size_bytes": len(encoded_part),
                    }
                    compound_searchable.append(part.text)
                else:
                    part_dict = {"type": "text", "text": part.text}
                if part.label:
                    part_dict["label"] = part.label
                materialized_parts.append(part_dict)
            elif isinstance(part, CompoundBlobRefPart):
                if not object_exists(ctx.vault_dir, part.blob_hash):
                    msg = (
                        f"compound part {idx}: blob_hash {part.blob_hash!r} "
                        "not found in object store"
                    )
                    raise InvalidateTargetError(msg)
                materialized_parts.append(
                    {
                        "type": "blob-ref",
                        "blob_hash": part.blob_hash,
                        "size_bytes": object_plaintext_size(ctx.vault_dir, part.blob_hash),
                        "mime": part.mime,
                        "filename_hint": part.filename_hint,
                        "label": part.label,
                    }
                )
        payload = build_compound_payload(
            parts=materialized_parts,
            context=context,
            type_hint=type_hint,
        )
        if compound_searchable:
            searchable_body = "\n".join(compound_searchable)
    else:  # BlobRefContent — bytes already in the object store via
        # /internal/blob/upload. Validate the hash exists; reject otherwise
        # so we don't write a dangling event row.
        if not object_exists(ctx.vault_dir, content.blob_hash):
            msg = f"blob_hash {content.blob_hash!r} not found in object store"
            raise InvalidateTargetError(msg)
        size_bytes = object_plaintext_size(ctx.vault_dir, content.blob_hash)
        payload = build_blob_ref_payload(
            blob_hash=content.blob_hash,
            size_bytes=size_bytes,
            mime=content.mime,
            filename_hint=content.filename_hint,
            context=context,
            type_hint=type_hint,
        )

    # Preserve any over-long context/type_hint originals losslessly in the
    # payload (write-first truncate-preserve, §3b). ``<field>_full`` mirrors the
    # observe convention; the truncated primary field stays the FTS-indexed one.
    if context_full is not None:
        payload["context_full"] = context_full
    if type_hint_full is not None:
        payload["type_hint_full"] = type_hint_full

    # Single-pass write: ``write_event_with_status`` returns the row plus a
    # was_inserted bool, so we don't have to compute the content hash twice
    # (Perf audit minor — eliminated the preview-hash SHA-256 + canonical_json
    # that the old code ran ahead of the actual write).
    event, was_inserted = write_event_with_status(
        db,
        origin=DEFAULT_ORIGIN,
        kind="remember",
        payload=payload,
        parent_hashes=parent_hashes,
        searchable_body=searchable_body,
    )
    if was_inserted:
        pe.record(
            db,
            event_id=event.id,
            event_hash=event.content_hash,
            stage=pe.STAGE_EVENT_WRITTEN,
            producer="remember",
        )
        schedule_extraction(event.id)
    already_existed = not was_inserted

    # Stamp server-authoritative client provenance (ADR-0006). OUTSIDE the
    # ``if was_inserted`` block on purpose: a dedup'd write from a DIFFERENT
    # client must still record that this client wrote (touched) the event.
    _stamp_provenance(db, event.id, "remember")

    # Write the invalidations AFTER the main write. Targets were already
    # validated (existence + not-itself-an-invalidation) before the content
    # write above, and nothing between mutates them, so this loop only writes.
    invalidated_ok: list[str] = []
    for target_hash in invalidates or []:
        # Reason: pull from the new event's content if text, else generic.
        reason = (
            content.text if isinstance(content, TextContent) else f"superseded by event {event.id}"
        )
        write_invalidation(db, target_hash=target_hash, reason=reason, origin=DEFAULT_ORIGIN)
        invalidated_ok.append(target_hash)

    return RememberResult(
        ok=True,
        event_id=event.id,
        content_hash=event.content_hash,
        deduplicated=already_existed,
        invalidated=invalidated_ok,
    )


# ── recall ──────────────────────────────────────────────────────────────────


# Compiled at import time; matches ULID-shaped strings.
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$", re.IGNORECASE)
_IDENTIFIER_PREFIXES = ("sha256:", "http://", "https://", "file://")


def _auto_route_depth(query: str) -> Depth:
    """Pick the optimal recall depth without bothering the caller."""
    stripped = query.strip()
    if not stripped:
        return "shallow"
    if any(stripped.startswith(p) for p in _IDENTIFIER_PREFIXES):
        return "shallow"
    if _ULID_RE.match(stripped):
        return "shallow"
    tokens = FTS5_SPECIALS_RE.sub(" ", stripped).split()
    if len(tokens) <= 1:
        return "shallow"
    return "normal"


# Queries that explicitly ask for the *current* state. When present, recall
# re-ranks the (already relevance-filtered) matches newest-first, so "current
# role" surfaces the latest record instead of an equally-text-matching older
# one. Non-temporal queries are untouched — no ranking regression.
_TEMPORAL_INTENT_RE = re.compile(
    r"\b(current|currently|latest|now|today|recent|recently|nowadays|present|"
    r"these days|right now|as of)\b",
    re.IGNORECASE,
)


def _has_temporal_intent(query: str) -> bool:
    return bool(_TEMPORAL_INTENT_RE.search(query))


def _recency_rerank(events: list[Event]) -> list[Event]:
    """Stable sort newest-first by created_at.

    Applied ONLY when the query shows temporal intent. All ``events`` already
    matched the query terms (FTS/vec), so among relevant matches "newest first"
    is the right answer for a "current/latest X" question. Pure FTS has no
    recency notion — this is the targeted fix for that gap (found by the recall
    benchmark: temporal queries scored hit@1=0 before this).
    """

    def _key(e: Event) -> datetime:
        try:
            dt = datetime.fromisoformat(e.created_at)
        except (ValueError, TypeError):
            return datetime.min.replace(tzinfo=UTC)
        # Normal writes are tz-aware (_now_iso), but rows written with an
        # explicit created_at= (backfills/eval) can be naive. Comparing a naive
        # dt against the tz-aware fallback (or another aware row) inside sorted()
        # raises TypeError OUTSIDE this function's try/except → recall 500.
        # Normalize naive → UTC so the key is always aware and comparable.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt

    return sorted(events, key=_key, reverse=True)


# Floor applied to a memory the user actually superseded (a real invalidation
# event), mirroring the temporal "superseded" floor. The authoritative signal,
# not the worker's inferred class.
_INVALIDATED_FLOOR = 0.15


def _temporal_rerank(
    events: list[Event], db: sqlite3.Connection, invalidated: set[str] | None = None
) -> list[Event]:
    """Stable re-rank scaling each hit's position by its temporal relevance.

    Phase 2 of the relevance-decay design. Reads the temporal layer in one
    batch; hits with no temporal record yet (most, until the worker catches up)
    get factor 1.0 and keep their place, while decayed one-offs and superseded
    facts sink. A memory carrying a real invalidation is floored regardless of
    its inferred class, so the authoritative supersession signal wins. Never
    excludes anything — decay is a recall score, not a delete (I2). The caller
    skips this for depth="deep", the flat history lens.
    """
    if len(events) < 2:
        return events
    invalidated = invalidated or set()
    temporal_map = read_event_temporal_batch(db, [e.content_hash for e in events])
    if not temporal_map and not invalidated:
        return events
    now = datetime.now(UTC)
    n = len(events)
    scored: list[tuple[float, int, Event]] = []
    for i, e in enumerate(events):
        base = float(n - i)  # incoming order is the baseline
        if e.content_hash in invalidated:
            factor = _INVALIDATED_FLOOR
        else:
            record = temporal_map.get(e.content_hash)
            factor = temporal_relevance(record, now) if record is not None else 1.0
        scored.append((base * factor, i, e))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [e for _, _, e in scored]


def _build_temporal_overlay(
    events: list[Event], db: sqlite3.Connection
) -> dict[str, dict[str, Any]]:
    """Per-hit temporal annotation (class + current relevance) for the
    interpretation dict, so the AI can SEE why a memory ranked where it did.
    Merged into the entity overlay; absent for hits with no temporal record."""
    if not events:
        return {}
    temporal_map = read_event_temporal_batch(db, [e.content_hash for e in events])
    if not temporal_map:
        return {}
    now = datetime.now(UTC)
    overlay: dict[str, dict[str, Any]] = {}
    for e in events:
        record = temporal_map.get(e.content_hash)
        if record is None:
            continue
        overlay[e.content_hash] = {
            "temporal_class": record.temporal_class,
            "temporal_relevance": round(temporal_relevance(record, now), 3),
        }
    return overlay


# Caveats layer thresholds. A topic whose *newest* matching event is older
# than this is flagged as possibly out of date. THIN = at most this many hits
# before recall admits "the vault may not hold this yet."
STALENESS_CAVEAT_DAYS = 30
THIN_EVIDENCE_MAX_HITS = 1

# An edge served below this confidence AND still `proposed` (unreviewed) is a
# low-confidence belief the coverage layer flags as tentative (ADR-0004 C3).
LOW_CONFIDENCE_EDGE_CAVEAT_THRESHOLD = 0.5


def _count_low_confidence_edges(overlay: dict[str, dict[str, Any]] | None) -> int:
    """Count served, unreviewed relations below the caveat threshold across the
    hits. Only `proposed` edges count — a confirmed / auto-confirmed edge below
    the threshold is not "tentative", it has already been judged."""
    if not overlay:
        return 0
    count = 0
    for fields in overlay.values():
        for edge in fields.get("entity_edges") or []:
            conf = edge.get("confidence")
            if (
                edge.get("trust") == "proposed"
                and isinstance(conf, (int, float))
                and conf < LOW_CONFIDENCE_EDGE_CAVEAT_THRESHOLD
            ):
                count += 1
    return count


def _compute_coverage(
    events: list[Event],
    invalidations: dict[str, Any],
    conflicts: dict[str, list[dict[str, Any]]],
    overlay: dict[str, dict[str, Any]] | None = None,
) -> RecallCoverage:
    """The honesty layer — what the vault does NOT confidently tell you.

    Computed entirely from signals the hits already carry (created_at,
    conflict verdicts, invalidation), no extra LLM call. Surfaces additively
    on the recall result so an AI client can hedge instead of treating thin,
    stale, or contradicted memory as settled fact. (BUILD #1 — informed by
    GBrain's gap analysis, adapted to afair's verdict taxonomy.)
    """
    caveats: list[str] = []

    # Thin evidence — the vault likely doesn't hold this yet.
    thin = len(events) <= THIN_EVIDENCE_MAX_HITS
    if not events:
        return RecallCoverage(
            caveats=["No matching memory. The vault likely doesn't hold this yet."],
            thin_evidence=True,
        )
    if thin:
        caveats.append(
            "Thin evidence: only a single matching record; the vault may not hold much on this yet."
        )

    # Staleness — age of the MOST RECENT matching event.
    newest_days: int | None = None
    now = datetime.now(UTC)
    ages = []
    for e in events:
        try:
            ages.append((now - datetime.fromisoformat(e.created_at)).days)
        except (ValueError, TypeError):
            continue
    if ages:
        newest_days = min(ages)  # smallest age = most recent event
        if newest_days >= STALENESS_CAVEAT_DAYS:
            caveats.append(
                f"Possibly out of date: even the most recent matching record is "
                f"{newest_days} days old."
            )

    # Unresolved contradictions among the returned hits (verdict-aware, so a
    # temporal update is NOT counted as a conflict). Also collect the distinct
    # user-facing caveat templates for the verdicts present.
    unresolved = 0
    seen_caveats: set[str] = set()
    for e in events:
        flags = conflicts.get(e.content_hash) or []
        hit_has_unresolved = False
        for c in flags:
            verdict = str(c.get("verdict", ""))
            if is_unresolved_conflict(verdict):
                hit_has_unresolved = True
            cav = _verdict_meta(verdict).caveat
            if cav:
                seen_caveats.add(cav)
        if hit_has_unresolved:
            unresolved += 1
    if unresolved:
        caveats.append(f"{unresolved} returned record(s) are in unresolved tension with another.")
    # Surface the distinct verdict caveats (e.g. "a newer record supersedes an
    # older one", "share a name but appear to be different things").
    caveats.extend(sorted(seen_caveats))

    # Invalidated hits (superseded/contradicted by a later event).
    invalidated = sum(1 for e in events if e.content_hash in invalidations)
    if invalidated:
        caveats.append(
            f"{invalidated} returned record(s) were later superseded; check the invalidation note."
        )

    # Low-confidence relations (ADR-0004): served, unreviewed edges below the
    # caveat threshold are surfaced WITH a caveat (recall honesty), not hidden —
    # and they feed the correction-on-recall loop.
    low_conf_edges = _count_low_confidence_edges(overlay)
    if low_conf_edges:
        caveats.append(
            f"{low_conf_edges} relation(s) in these results are low-confidence beliefs; "
            "treat as tentative."
        )

    return RecallCoverage(
        caveats=caveats,
        stale_newest_event_days=newest_days,
        unresolved_contradictions=unresolved,
        invalidated_hits=invalidated,
        thin_evidence=thin,
        low_confidence_edges=low_conf_edges,
    )


def _article_first_order(events: list[Event], invalidated: set[str] | None = None) -> list[Event]:
    """Stable-partition entity_article hits to the front of a query result.

    An article only appears in a query's results when it matched (FTS / vec
    / entity-name), and an article is a dense synthesis of exactly that
    entity — so when one is relevant the caller should read it before the
    raw events it summarizes. This is the recall side of the Karpathy
    LLM-Wiki / RAG-bypass: prefer the synthesis. Order within each partition
    (and thus the underlying fused ranking) is preserved.

    Superseded articles are dropped entirely. The article worker now
    deletes the old FTS row on re-synthesis, but pre-fix stale rows and the
    re-synthesis race window can still surface a dead version. A stale
    article must never lead — let alone fill — recall, so any article whose
    content_hash is invalidated is removed here. The current version also
    matched and stays. Non-article invalidated events are not the concern
    of this function; they remain (annotated) for history.
    """
    invalidated = invalidated or set()
    articles = [
        e for e in events if e.kind == ENTITY_ARTICLE_KIND and e.content_hash not in invalidated
    ]
    rest = [e for e in events if e.kind != ENTITY_ARTICLE_KIND]
    # Drop invalidated articles from the result set as well (they are in
    # neither partition above) — fall through to rest-only when none remain.
    if not articles:
        return rest if any(e.kind == ENTITY_ARTICLE_KIND for e in events) else events
    return articles + rest


def recall(
    query: str | None = None,
    scope: str | None = None,
    depth: Depth = "auto",
    limit: int | None = None,
    by_id: str | None = None,
    by_content_hash: str | None = None,
    full_payload: bool = False,
    stats: bool = False,
    feedback: RecallFeedback | None = None,
    decide: CorrectionDecision | list[CorrectionDecision] | None = None,
    pending_limit: int | None = None,
    pending_offset: int = 0,
    verbosity: RecallVerbosity = "compact",
    cursor: str | None = None,
) -> RecallResult:
    """Read the vault. Six call modes share this signature:

      recall(query=...)                          → semantic search
      recall(by_id=...)                          → single-event lookup
      recall(by_content_hash=...)                → single-event lookup
      recall(stats=True)                         → summary + recent hits
      recall(query=..., full_payload=True)       → search, untruncated
      recall()                                   → most-recent N hits

    ``stats=True`` can combine with any of the above to add a
    ContextSummary to the response.

    Lookup modes (``by_id``/``by_content_hash``) imply ``full_payload=True``
    semantically — when you ask for one specific event you want it whole, and
    they always serve the ``full`` shape regardless of ``verbosity``.

    ``verbosity`` (default ``compact``) shapes how much of each hit's
    interpretation/conflicts/list detail is served; it is orthogonal to
    ``full_payload`` (payload materialization). ``limit`` (omitted → 10 compact /
    20 otherwise) is clamped to ``MAX_RECALL_LIMIT``. ``cursor`` pages
    search/browse results best-effort — pass the returned ``next_cursor`` back
    verbatim; rankings are recomputed per call.

    ``scope`` is a free-text substring matched against the interpretation's
    topic_signal (Phase 3.5 — currently no-op until topic_signal lands).
    """
    if by_id is not None and by_content_hash is not None:
        msg = "recall accepts at most one of by_id, by_content_hash"
        raise InvalidRecallArgsError(msg)

    db = connect_for_thread()
    ctx = get_context()
    note: str | None = None

    # Effective page size (P1-2): omitted limit defaults to 10 in compact, 20
    # otherwise; any explicit value is clamped to [1, MAX_RECALL_LIMIT].
    eff_limit = (
        limit
        if limit is not None
        else (COMPACT_DEFAULT_LIMIT if verbosity == "compact" else DEFAULT_RECALL_LIMIT)
    )
    eff_limit = max(1, min(eff_limit, MAX_RECALL_LIMIT))

    # Cursor paging (P1-2): a best-effort byte offset into the recomputed
    # ranking. Parse leniently — a bad cursor never fails a recall, it just
    # serves page 1 with a note.
    offset = 0
    if cursor is not None:
        try:
            offset = max(0, min(int(cursor), MAX_RECALL_OFFSET))
        except (TypeError, ValueError):
            note = _combine_notes(note, "ignored malformed cursor")

    # Recall-feedback signal (additive optional arg per I1). Written
    # to tuner_state as kind='observation' for the tuner to read
    # later. Never blocks the recall itself — if the write fails,
    # we log and continue. Best-effort signal collection.
    if feedback is not None:
        _record_recall_feedback(db, feedback)

    # Operator confirm/reject on an entity-audit proposal (additive optional
    # arg per I1, like feedback). Applied synchronously so the same turn can
    # tell the user "done"; the outcome rides back on `note`. Kept in its own
    # variable so the query path's note reassignments can't clobber it on a
    # combined recall(decide=..., query=...) call.
    decisions_out: list[CorrectionOutcomeView] = []
    decide_note: str | None = None
    if decide is not None:
        batch = decide if isinstance(decide, list) else [decide]
        if len(batch) > MAX_DECIDE_BATCH:
            msg = f"decide accepts at most {MAX_DECIDE_BATCH} decisions per call; got {len(batch)}"
            raise ValueError(msg)
        for d in batch:
            try:
                outcome = decide_correction(
                    db, proposal_id=d.proposal_id, verdict=d.verdict, to_kind=d.to_kind
                )
                decisions_out.append(
                    CorrectionOutcomeView(
                        proposal_id=outcome.proposal_id, status=outcome.status, note=outcome.note
                    )
                )
            except ValueError as exc:
                # One bad decision must not void the rest of an operator batch;
                # surfaced per-item, never silently swallowed. Only ValueError
                # (validation: bad verdict/to_kind) is isolated this way — a
                # deeper fault (e.g. sqlite3.Error) still aborts mid-batch, but
                # decide_correction is idempotent, so re-sending the batch yields
                # `already_decided` for the ones that landed and retries the rest.
                decisions_out.append(
                    CorrectionOutcomeView(proposal_id=d.proposal_id, status="error", note=str(exc))
                )
        applied = sum(1 for o in decisions_out if o.status in ("applied", "confirmed", "rejected"))
        if len(batch) == 1:
            # Preserve the single-decision note format shipped clients read:
            # "<label> <verdict>: <note>". decide_correction dispatches
            # ont_-prefixed ids to the ontology queue (ADR-0003 Phase 5).
            single = batch[0]
            label = (
                "ontology revision"
                if single.proposal_id.startswith(ONTOLOGY_PROPOSAL_ID_PREFIX)
                else "correction"
            )
            decide_note = f"{label} {single.verdict}: {decisions_out[0].note}"
        else:
            decide_note = f"{applied}/{len(batch)} corrections decided"

    summary: ContextSummary | None = None
    if stats:
        summary = _build_stats_summary(db)

    # Surface the open audit queue on the session-start/check-in call
    # (stats=True), after a decision so the client sees what remains, and
    # whenever the caller explicitly pages it (pending_limit set). Pagination is
    # additive per I1: a client draining the queue asks for a page, decides it,
    # then re-fetches at pending_offset=0 (deciding removes rows from the open
    # set, so advancing the offset would skip the new head — see the helper).
    include_pending = stats or decide is not None or pending_limit is not None
    # Clamp on BOTH ends: an unbounded lower edge let pending_limit=-5 reach
    # SQLite as LIMIT -5 (unlimited) + a Python slice [:-5], bypassing the cap
    # for a >200-row queue. max(0, ...) makes a negative page an empty page.
    eff_pending_limit = max(
        0,
        min(
            pending_limit if pending_limit is not None else DEFAULT_PENDING_LIMIT, MAX_PENDING_LIMIT
        ),
    )
    eff_pending_offset = max(0, pending_offset)
    pending: list[ProposedCorrectionView] = (
        _pending_correction_views(db, limit=eff_pending_limit, offset=eff_pending_offset)
        if include_pending
        else []
    )

    # The cheap universal nudge: the TRUE open-queue total on every recall,
    # so a client can prompt "you have N memories to review" without the
    # operator ever calling stats=True. The heavy list stays gated above.
    pending_count = count_pending_corrections(db) + count_pending_ontology_proposals(db)

    # ── Single-event lookup mode ───────────────────────────────────────────
    if by_id is not None or by_content_hash is not None:
        target = (
            read_event_by_id(db, by_id)
            if by_id is not None
            else read_event_by_hash(db, by_content_hash)  # type: ignore[arg-type]
        )
        if target is None:
            selector = (
                f"by_id={by_id!r}" if by_id is not None else f"by_content_hash={by_content_hash!r}"
            )
            return RecallResult(
                hits=[],
                depth_used="shallow",
                note=_combine_notes(decide_note, note or f"no event found for {selector}"),
                summary=summary,
                pending_corrections=pending,
                pending_corrections_count=pending_count,
                decisions=decisions_out,
            )
        invalidations = _attach_invalidations([target], db)
        conflicts = _attach_conflicts([target], db)
        overlay = _build_entity_overlay([target], db)
        interp_map = read_latest_interpretations_batch(db, [target.content_hash])
        linked_map = get_linked_event_ids_batch(db, [target.content_hash])
        # Provenance (ADR-0006): by_id/by_content_hash always serve the full
        # shape, so always surface the writing client.
        provenance_map = read_event_provenance_batch(db, [target.id])
        # Durability rationale (W2): by_id/by_content_hash serve verbosity="full",
        # so always fetch the salience overlay for this single event.
        salience_map = read_latest_salience_batch(db, [target.content_hash])
        interp_for_target = interp_map.get(target.content_hash)
        return RecallResult(
            hits=[
                _event_to_hit(
                    target,
                    db,
                    full_payload=True,  # lookup-by-id always returns the full event
                    invalidation=invalidations.get(target.content_hash),
                    conflicts=conflicts.get(target.content_hash),
                    entity_overlay=overlay.get(target.content_hash),
                    interpretation_extraction=(
                        interp_for_target.extraction if interp_for_target else None
                    ),
                    salience_extraction=salience_map.get(target.content_hash),
                    linked_event_ids=linked_map.get(target.content_hash, []),
                    client=_earliest_client(provenance_map.get(target.id)),
                    # by_id/by_content_hash are the re-fetch escape hatch: always
                    # serve the complete shape regardless of the verbosity arg.
                    verbosity="full",
                )
            ],
            depth_used="shallow",
            note=_combine_notes(decide_note, note),
            summary=summary,
            pending_corrections=pending,
            pending_corrections_count=pending_count,
            decisions=decisions_out,
        )

    # ── Search / browse mode ───────────────────────────────────────────────
    events: list[Event]
    depth_used: Depth = depth

    # Overfetch by (offset + page + 1): the +1 makes has_more exact, the offset
    # covers the skipped prefix. Bounded so a huge cursor can't fan out the
    # underlying scans. Everything below fetches fetch_n, then we slice the
    # requested window after all reordering.
    fetch_n = min(offset + eff_limit + 1, MAX_RECALL_OFFSET + MAX_RECALL_LIMIT + 1)

    if query is None or not query.strip():
        # No query, no by_id — return most-recent N events (browse mode).
        events = list(iter_events(db, limit=fetch_n))
        depth_used = "shallow"
        invalidations = _attach_invalidations(events, db)
    else:
        # Real query — resolve depth, run FTS+vec if normal.
        if depth == "auto":
            depth = _auto_route_depth(query)
            depth_used = depth

        # Phase 4 Track 1 Stage 4 — entity-aware routing. Auto-detect: if
        # the query string matches a canonical entity name or a known
        # surface form, pull those events as a third ranking signal.
        # Returns [] when nothing matches — pure no-op for non-entity
        # queries.
        entity_hits = _events_via_entity_match(db, query, limit=fetch_n)

        if depth == "shallow" or not ctx.semantic_recall_enabled:
            fts_hits = search_fts(db, query, limit=fetch_n)
            events = rrf_merge(fts_hits, entity_hits, limit=fetch_n) if entity_hits else fts_hits
            depth_used = "shallow"
        else:
            api_key = _api_key_for_embedding(ctx)
            emb_future = _RECALL_POOL.submit(
                embed_query, model=ctx.embedding_model, text=query, api_key=api_key
            )
            fts_hits = search_fts(db, query, limit=fetch_n)
            try:
                embedding = emb_future.result()
            except EmbeddingError:
                events = (
                    rrf_merge(fts_hits, entity_hits, limit=fetch_n) if entity_hits else fts_hits
                )
                depth_used = "shallow"
                note = _combine_notes(
                    note, "semantic recall unavailable; returned FTS-only results"
                )
            else:
                vec_hits = search_vec(db, embedding, limit=fetch_n)
                # Fuse FTS+vec first (hybrid baseline), then layer in the
                # entity-match boost. Double-counted appearances naturally
                # rank higher — events that BOTH match by name AND match
                # by text/embedding deserve the front of the list.
                hybrid = rrf_merge(fts_hits, vec_hits, limit=fetch_n)
                events = rrf_merge(hybrid, entity_hits, limit=fetch_n) if entity_hits else hybrid
                depth_used = "normal"
                if depth == "deep":
                    note = _combine_notes(
                        note,
                        "deep depth is not yet richer than normal "
                        "(Phase 3+ reasoning agent pending); returned hybrid results",
                    )

        # Temporal intent ("current role", "latest …") → prefer the newest
        # matching record. Only kicks in for temporal queries; everything else
        # keeps its relevance ranking. (Closes the recency gap the recall
        # benchmark surfaced.)
        if query and _has_temporal_intent(query):
            events = _recency_rerank(events)

        # Invalidations first — used by both the temporal decay (an actually-
        # superseded memory is the authoritative "stale" signal) and the
        # article-first hoist below. Reordering never changes content hashes,
        # so this set stays valid through both steps.
        invalidations = _attach_invalidations(events, db)

        # Temporal relevance (Phase 2): de-prioritize memories whose moment has
        # passed (a closed deadline, last month's dinner) so settled things
        # don't crowd out what's live. A memory the user actually invalidated is
        # floored here too, so the real supersession signal wins over the
        # worker's inferred "superseded" class. Decay is a recall score, never a
        # delete (I2); history stays findable. depth="deep" is the flat history
        # lens that bypasses it.
        if depth != "deep":
            events = _temporal_rerank(events, db, invalidated=set(invalidations))

        # Article-first: when an entity article matched, surface the dense
        # synthesis before the raw events it summarizes (query path only —
        # browse mode stays chronological).
        events = _article_first_order(events, invalidated=set(invalidations))

    # Cursor slice (P1-2): the searches overfetched by (offset + page + 1), and
    # invalidations were computed on that fuller list above (a dict superset is
    # harmless). Now cut the requested window so every per-hit cost below —
    # conflicts, overlay, interp/linked batches, coverage — stays page-sized.
    # The +1 overfetch makes has_more exact.
    has_more = len(events) > offset + eff_limit
    events = events[offset : offset + eff_limit]
    # Terminate paging at the offset cap: the cursor parser clamps any incoming
    # value to MAX_RECALL_OFFSET, so emitting a next_cursor beyond it would make
    # a client that pages "until next_cursor is None" loop forever (the clamp
    # keeps re-serving the last honorable window). Stop with a note instead.
    next_offset = offset + eff_limit
    if has_more and next_offset <= MAX_RECALL_OFFSET:
        next_cursor = str(next_offset)
    else:
        next_cursor = None
        if has_more:
            note = _combine_notes(note, "result window capped; refine the query to see more")

    # invalidations is computed per-branch above (browse + query) and reused
    # here so the per-hit annotation does not re-query.
    conflicts = _attach_conflicts(events, db)
    overlay = _build_entity_overlay(events, db)
    # Layer the temporal annotation (class + current relevance) into the same
    # overlay so it rides into each hit's interpretation, visible to the AI.
    for chash, temporal_fields in _build_temporal_overlay(events, db).items():
        overlay.setdefault(chash, {}).update(temporal_fields)
    # Batch the per-hit DB calls that previously ran N+1 (Perf audit C3).
    # Two queries replace 2*N queries for N hits — biggest single win on
    # the recall hot path.
    event_hashes = [e.content_hash for e in events]
    interp_map = read_latest_interpretations_batch(db, event_hashes)
    linked_map = get_linked_event_ids_batch(db, event_hashes)
    # Provenance (ADR-0006): served standard/full only. Compact drops the
    # ``client`` field from the wire anyway (exclude_none), so skip the batch
    # query entirely on the compact hot path — one indexed lookup saved.
    provenance_map = (
        read_event_provenance_batch(db, [e.id for e in events]) if verbosity != "compact" else {}
    )
    # Durability rationale (W2): merged only at verbosity="full", so compact and
    # standard add ZERO queries here — the salience overlay is fetched only when
    # it will actually be served.
    salience_map = read_latest_salience_batch(db, event_hashes) if verbosity == "full" else {}
    return RecallResult(
        hits=[
            _event_to_hit(
                e,
                db,
                full_payload=full_payload,
                invalidation=invalidations.get(e.content_hash),
                conflicts=conflicts.get(e.content_hash),
                entity_overlay=overlay.get(e.content_hash),
                interpretation_extraction=(
                    interp_map[e.content_hash].extraction if e.content_hash in interp_map else None
                ),
                salience_extraction=salience_map.get(e.content_hash),
                linked_event_ids=linked_map.get(e.content_hash, []),
                client=_earliest_client(provenance_map.get(e.id)),
                verbosity=verbosity,
            )
            for e in events
        ],
        depth_used=depth_used,
        note=_combine_notes(decide_note, note),
        summary=summary,
        coverage=_compute_coverage(events, invalidations, conflicts, overlay),
        pending_corrections=pending,
        pending_corrections_count=pending_count,
        decisions=decisions_out,
        next_cursor=next_cursor,
    )


def _build_stats_summary(db: Any) -> ContextSummary:
    """Compute the vault-wide totals + breakdowns for stats=True.

    Single scan: groups by (kind, origin) at the DB and rolls up to the
    three views in Python. The previous version ran three independent
    queries (one COUNT, two GROUP BYs) — same data, twice the disk reads
    on a cold cache. Perf audit minor.
    """
    rows = db.execute("SELECT kind, origin, COUNT(*) AS c FROM events GROUP BY kind, origin")
    total = 0
    by_kind: dict[str, int] = {}
    by_origin: dict[str, int] = {}
    for row in rows:
        c = row["c"]
        total += c
        by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + c
        by_origin[row["origin"]] = by_origin.get(row["origin"], 0) + c
    # Provenance breakdown (ADR-0006): a different axis from by_origin — which
    # writing CLIENT touched the vault. Empty on a pre-provenance vault.
    by_client = count_events_by_client(db)
    return ContextSummary(
        total_events=total, by_kind=by_kind, by_origin=by_origin, by_client=by_client
    )


def _combine_notes(*parts: str | None) -> str | None:
    """Join the non-empty note fragments (e.g. a decide outcome + a depth
    fallback) into one ``note``, or None when there's nothing to say."""
    kept = [p for p in parts if p]
    return "; ".join(kept) if kept else None


def _pending_correction_views(
    db: Any, *, limit: int = 20, offset: int = 0
) -> list[ProposedCorrectionView]:
    """Open entity-audit AND ontology proposals, mapped to the recall wire
    model. One list, one decide loop (ADR-0003 Phase 5): ontology proposals
    carry ``kind='ontology_<action>'`` and dispatch on their ``ont_`` id
    prefix server-side, so the client treats every row identically.

    Pagination: each source is fetched to ``offset + limit`` (entity-audit
    first, ontology second — the existing order), the two are concatenated,
    then the window ``[offset : offset + limit]`` is sliced. Queues are at most
    a few hundred rows, so slicing the concatenation in Python is simpler and
    correct versus threading a cross-table offset through SQL.

    Drain semantics: deciding a proposal removes it from the open set, so a
    client working the queue down should re-fetch at ``pending_offset=0`` after
    each decide batch rather than advancing the offset (advancing would skip the
    rows that shifted into the head)."""
    fetch = offset + limit
    views = [
        ProposedCorrectionView(
            id=p.id,
            kind=p.kind,
            entity_id=p.entity_id,
            entity_name=p.entity_name,
            prompt=p.prompt,
            evidence=p.evidence,
            confidence=p.confidence,
        )
        for p in read_pending_corrections(db, limit=fetch)
    ]
    views += [
        ProposedCorrectionView(
            id=p.id,
            kind=f"ontology_{p.action}",
            prompt=p.prompt,
            evidence=p.evidence,
            confidence=p.confidence,
            subject_slug=p.subject_slug,
        )
        for p in read_pending_ontology_proposals(db, limit=fetch)
    ]
    return views[offset : offset + limit]


# ── observe ─────────────────────────────────────────────────────────────────


def observe(event: ObserveEvent) -> ObserveResult:
    db = connect_for_thread()

    event_dict = event.model_dump(exclude_none=False)
    # Reserved payload keys always win over caller-supplied extras.
    # ObserveEvent allows arbitrary extras, so a caller could otherwise
    # smuggle content_type="text-large" + blob_hash=<existing hash> and
    # make the extractor rehydrate an unrelated blob as this event's
    # body. content_type is pinned to "event"; the modality-dispatch
    # keys (blob_hash, text, parts) are stripped from extras.
    for reserved in ("content_type", "blob_hash", "text", "parts"):
        event_dict.pop(reserved, None)
    payload: dict[str, Any] = {**event_dict, "content_type": "event"}

    # Single-pass write — see remember() for the rationale. Skips the
    # preview-hash compute_content_hash + read_event_by_hash that the old
    # code ran ahead of the actual write.
    written, was_inserted = write_event_with_status(
        db,
        origin=DEFAULT_ORIGIN,
        kind="observe",
        payload=payload,
    )
    if was_inserted:
        pe.record(
            db,
            event_id=written.id,
            event_hash=written.content_hash,
            stage=pe.STAGE_EVENT_WRITTEN,
            producer="observe",
        )
        schedule_extraction(written.id)

    # Stamp client provenance (ADR-0006) — outside the was_inserted guard so a
    # dedup'd observe from a different client still records the write.
    _stamp_provenance(db, written.id, "observe")

    return ObserveResult(
        ok=True,
        event_id=written.id,
        content_hash=written.content_hash,
    )
