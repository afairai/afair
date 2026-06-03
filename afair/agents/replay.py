"""
Replay infrastructure — re-run a worker on past events with two
parameter sets (current production + candidate variant) and collect
matched outputs for the judge.

Used by the tuner before any promote. Replay is **offline** and
**read-only** against the substrate: it does NOT write
interpretations, does NOT mutate state. It just computes outputs in
memory for comparison.

Per the plan §6.1 "Replay module" — pulls last N events of the
relevant kind, runs through both variants, returns paired outputs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from ..substrate.events import iter_events

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable


log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ReplayPair:
    """One event, scored under both parameter sets."""
    event_id: str
    content_hash: str
    input_summary: str  # short human-readable preview of the input
    output_current: Any
    output_variant: Any


def replay_with_variants(
    conn: sqlite3.Connection,
    *,
    scoring_fn: Callable[[sqlite3.Connection, Any, dict[str, Any]], Any],
    current_params: dict[str, Any],
    variant_params: dict[str, Any],
    kind_filter: tuple[str, ...] = ("remember", "observe"),
    sample_size: int = 30,
) -> list[ReplayPair]:
    """Pull the most recent ``sample_size`` events of the given kinds
    and score each with both parameter sets.

    ``scoring_fn`` is a worker-specific function with signature
    ``(conn, event, params) -> output``. Examples:

      * salience: ``lambda c, e, p: score_event(c, e, weights=p["weights"])``
      * surprise: ``lambda c, e, p: _compute_surprise(...)`` with the
        candidate window size

    Returns one ReplayPair per event. Empty list if no events match
    the filter.
    """
    events = [e for e in iter_events(conn, limit=sample_size) if e.kind in kind_filter]
    pairs: list[ReplayPair] = []
    for event in events:
        try:
            out_current = scoring_fn(conn, event, current_params)
        except Exception as e:
            log.warning("replay.scoring_failed", which="current", event_id=event.id, error=str(e))
            continue
        try:
            out_variant = scoring_fn(conn, event, variant_params)
        except Exception as e:
            log.warning("replay.scoring_failed", which="variant", event_id=event.id, error=str(e))
            continue
        pairs.append(
            ReplayPair(
                event_id=event.id,
                content_hash=event.content_hash,
                input_summary=_summarize_event(event),
                output_current=out_current,
                output_variant=out_variant,
            ),
        )
    log.info(
        "replay.completed",
        sample_size=sample_size,
        kept=len(pairs),
        kind_filter=kind_filter,
    )
    return pairs


def _summarize_event(event: Any) -> str:
    """Short human-readable preview of an event for the judge prompt."""
    payload = event.payload or {}
    content_type = payload.get("content_type", "unknown")
    parts = [f"kind={event.kind}", f"content_type={content_type}"]
    if hint := payload.get("type_hint"):
        parts.append(f"type_hint={hint}")
    # Add a snippet of the actual content for context.
    text = payload.get("text") if isinstance(payload, dict) else None
    if isinstance(text, str) and text:
        snippet = text[:200].replace("\n", " ")
        parts.append(f"text={snippet!r}")
    elif content_type == "event":
        action = payload.get("action")
        subject = payload.get("subject")
        if action:
            parts.append(f"action={action}")
        if subject:
            parts.append(f"subject={subject}")
    return " | ".join(parts)
