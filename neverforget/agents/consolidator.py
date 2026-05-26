"""Consolidator — daily theme summaries (Phase 3).

Pattern: at most once per UTC day, pick all the events for that day,
ask the LLM to write a coherent narrative + extract themes, write the
result back as a NEW substrate event with kind="consolidation". The
new event references its constituents via parent_hashes, so the
lineage is explicit and traversable.

Why a substrate event (not an interpretation row): consolidations are
themselves first-class durable content. They should be searchable via
FTS5, embeddable, recallable, and themselves interpretable. Making them
a regular substrate event with a different ``kind`` gets all that for
free — FTS indexes the summary text, the warm-path Extractor + Bind
agent process the consolidation like any other event, and recall
naturally surfaces "what was the week about" as a hit.

Loop prevention: the extractor skips kind="consolidation" — we don't
want the Extractor to LLM-summarize a summary, and we don't want the
Bind agent to chain consolidations together via embeddings into a
self-referential cluster. Both are out of scope for v0.

Bounded: one consolidation per UTC day, at most. The worker checks if
today (or yesterday — depending on time-of-run) already has a
consolidation; if yes, no-op. Idempotent.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel

from ..substrate import write_event
from .cold_path import ColdPathWorker
from .llm import LLMError, call_tool

if TYPE_CHECKING:
    import sqlite3

    from ..settings import Settings
    from ..substrate.events import Event

log = structlog.get_logger(__name__)


CONSOLIDATION_KIND = "consolidation"
"""New ``kind`` for substrate events created by this worker. Recall + FTS
treat them like any other event."""

CONSOLIDATION_PRODUCER = "consolidator:v0"
"""Tagged into the consolidation event's payload for lineage and so
future versions can be identified."""

MIN_EVENTS_FOR_CONSOLIDATION = 3
"""Days with fewer events skip consolidation — not enough material to
write a useful summary, and the existing recall over those events
already does the job."""

MAX_EVENTS_PER_CONSOLIDATION = 50
"""Hard cap on input to the LLM. A burst day (50+ events) gets the most
recent N — the rest stay individually queryable but don't bloat the
LLM prompt."""

LOOKBACK_DAYS = 2
"""On each run, consider TODAY (UTC) plus the past LOOKBACK_DAYS-1 days
in case the scheduler missed a window (deploy gap, machine restart).
Each day is checked independently for whether it already has a
consolidation."""


_TOOL_NAME = "record_daily_consolidation"
_TOOL_DESCRIPTION = (
    "Write a coherent narrative summary + extracted themes for one day's "
    "worth of substrate events. The summary becomes searchable content; "
    "themes are durable categories for emergent ontology (Phase 4)."
)
_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "narrative": {
            "type": "string",
            "description": (
                "Coherent prose summary of the day, 2-5 sentences. Reads "
                "as if YOU lived this day. Mentions the actors, decisions, "
                "and unresolved threads. Plain language."
            ),
        },
        "themes": {
            "type": "array",
            "description": (
                "Up to 6 short noun-phrases summarizing what the day was "
                "about. Themes are durable: 'phase 3 design', 'sajinth "
                "collab', 'security hardening'. They feed emergent ontology."
            ),
            "items": {"type": "string"},
            "maxItems": 6,
        },
        "open_threads": {
            "type": "array",
            "description": (
                "Up to 4 short descriptions of things that were started "
                "but not finished — questions left open, work in progress, "
                "decisions deferred. Helps future-AI pick up where today "
                "left off."
            ),
            "items": {"type": "string"},
            "maxItems": 4,
        },
    },
    "required": ["narrative", "themes"],
}

_SYSTEM_PROMPT = """\
You are a personal-vault consolidator. Once per day, you summarize that
day's events into a narrative + theme list + open threads. The summary
becomes part of the user's persistent memory.

Write in second person ("you decided", "you and Sajinth shipped X"),
present tense, plain language. Mention names, decisions, and any
unresolved threads. Don't pad — 2-5 sentences is plenty.

Use the record_daily_consolidation tool exactly once.
"""


class _DaySummary(BaseModel):
    """Pydantic shape mirroring the tool-call result."""

    narrative: str
    themes: list[str]
    open_threads: list[str] = []


class Consolidator(ColdPathWorker):
    """Once-per-day theme summarizer."""

    name = "consolidator"
    interval_seconds = 6 * 3600  # check 4 times per day; actual work guarded by day-key
    """Check four times a day so a deploy hiccup doesn't postpone the
    consolidation by 24h. Real work is gated by day-already-consolidated
    so the LLM call doesn't actually fire more than once per day."""

    def run(self, conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "days_checked": 0,
            "days_consolidated": 0,
            "days_skipped_few_events": 0,
            "days_skipped_already_done": 0,
            "llm_errors": 0,
        }
        model = settings.extractor_model
        api_key = _api_key_for_model(model, settings)

        today = datetime.now(UTC).date()
        for offset in range(LOOKBACK_DAYS):
            target = today - timedelta(days=offset)
            stats["days_checked"] += 1
            if _has_consolidation_for_day(conn, target):
                stats["days_skipped_already_done"] += 1
                continue
            events = _events_for_day(conn, target)
            if len(events) < MIN_EVENTS_FOR_CONSOLIDATION:
                stats["days_skipped_few_events"] += 1
                continue
            try:
                summary = _summarize_day(
                    target_day=target, events=events, model=model, api_key=api_key
                )
            except LLMError as e:
                log.warning("consolidator.llm_error", day=str(target), error=str(e))
                stats["llm_errors"] += 1
                continue
            _write_consolidation(conn, target_day=target, events=events, summary=summary)
            stats["days_consolidated"] += 1
            log.info(
                "consolidator.day_consolidated",
                day=str(target),
                event_count=len(events),
                theme_count=len(summary.themes),
            )

        return stats


def _api_key_for_model(model: str, settings: Settings) -> str | None:
    if model.startswith("anthropic/") and settings.anthropic_api_key is not None:
        return settings.anthropic_api_key.get_secret_value()
    if model.startswith("openai/") and settings.openai_api_key is not None:
        return settings.openai_api_key.get_secret_value()
    if model.startswith("gemini/") and settings.gemini_api_key is not None:
        return settings.gemini_api_key.get_secret_value()
    return None


def _has_consolidation_for_day(conn: sqlite3.Connection, day: date) -> bool:
    """Did we already write a consolidation event for this UTC day?

    Conservative check: ANY consolidation event whose payload's
    ``target_day`` field matches the ISO day. Multiple consolidation
    rows per day would be redundant; one is plenty.
    """
    row = conn.execute(
        """
        SELECT 1 FROM events
        WHERE kind = ?
          AND json_extract(payload, '$.target_day') = ?
        LIMIT 1
        """,
        (CONSOLIDATION_KIND, day.isoformat()),
    ).fetchone()
    return row is not None


def _events_for_day(conn: sqlite3.Connection, day: date) -> list[Event]:
    """Pull all events whose created_at falls within the UTC day.

    Excludes consolidation events themselves (loop prevention) and
    invalidation events (their own semantics, not appropriate as
    summary material).
    """
    from ..substrate.events import row_to_event

    start = datetime.combine(day, datetime.min.time(), tzinfo=UTC).isoformat()
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=UTC).isoformat()
    rows = conn.execute(
        """
        SELECT * FROM events
        WHERE created_at >= ?
          AND created_at < ?
          AND kind NOT IN (?, ?)
        ORDER BY created_at ASC
        LIMIT ?
        """,
        (start, end, CONSOLIDATION_KIND, "invalidate", MAX_EVENTS_PER_CONSOLIDATION),
    ).fetchall()
    return [row_to_event(r) for r in rows]


def _summarize_day(
    *,
    target_day: date,
    events: list[Event],
    model: str,
    api_key: str | None,
) -> _DaySummary:
    """One LLM call. Returns a structured summary."""
    brief = [_event_brief(e) for e in events]
    user_msg = (
        f"Day: {target_day.isoformat()} (UTC)\n"
        f"Event count: {len(events)}\n\n"
        f"Events (chronological):\n{json.dumps(brief, ensure_ascii=False, indent=2)}"
    )
    result = call_tool(
        model=model,
        system=_SYSTEM_PROMPT,
        user=user_msg,
        tool_name=_TOOL_NAME,
        tool_description=_TOOL_DESCRIPTION,
        tool_schema=_TOOL_SCHEMA,
        api_key=api_key,
        max_tokens=800,
    )
    data = result.data
    return _DaySummary(
        narrative=str(data.get("narrative", "")),
        themes=[str(t) for t in (data.get("themes") or [])],
        open_threads=[str(t) for t in (data.get("open_threads") or [])],
    )


def _event_brief(event: Event) -> dict[str, Any]:
    payload = event.payload
    return {
        "id": event.id,
        "kind": event.kind,
        "at": event.created_at,
        "content_type": payload.get("content_type"),
        "text": (payload.get("text") or "")[:600],
        "context": payload.get("context"),
        "action": payload.get("action"),
        "subject": payload.get("subject"),
        "result": payload.get("result"),
    }


def _write_consolidation(
    conn: sqlite3.Connection,
    *,
    target_day: date,
    events: list[Event],
    summary: _DaySummary,
) -> Event:
    """Write the consolidation as a NEW substrate event.

    parent_hashes carry the lineage — every constituent event is
    referenced, so the substrate's graph view shows the consolidation
    as a node aggregating its day. FTS indexes the narrative + themes
    so recall surfaces consolidations naturally.
    """
    payload = {
        "content_type": "text",
        "text": summary.narrative,
        "target_day": target_day.isoformat(),
        "themes": summary.themes,
        "open_threads": summary.open_threads,
        "event_count": len(events),
        "produced_by": CONSOLIDATION_PRODUCER,
        # Context field is FTS-indexed (per derive_searchable_text), so
        # the themes show up in keyword search too.
        "context": "Daily consolidation: " + ", ".join(summary.themes),
    }
    return write_event(
        conn,
        origin="agent",
        kind=CONSOLIDATION_KIND,
        payload=payload,
        parent_hashes=[e.content_hash for e in events],
    )
