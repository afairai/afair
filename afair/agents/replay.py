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


@dataclass(frozen=True)
class ReplayReport:
    """Outcome of a replay run — pairs plus failure counter so a
    silent drop never goes unnoticed."""

    pairs: list[ReplayPair]
    sample_size_requested: int
    sample_size_kept: int
    failed_current_count: int
    failed_variant_count: int

    @property
    def failed_any_count(self) -> int:
        return self.failed_current_count + self.failed_variant_count


def replay_with_variants(
    conn: sqlite3.Connection,
    *,
    scoring_fn: Callable[[sqlite3.Connection, Any, dict[str, Any]], Any],
    current_params: dict[str, Any],
    variant_params: dict[str, Any],
    kind_filter: tuple[str, ...] = ("remember", "observe"),
    sample_size: int = 30,
) -> ReplayReport:
    """Pull the most recent ``sample_size`` events of the given kinds
    and score each with both parameter sets.

    ``scoring_fn`` is a worker-specific function with signature
    ``(conn, event, params) -> output``. The ``output`` should be
    the FULL worker output (e.g., the salience extraction dict with
    score + components), not just a scalar — invariant guards in the
    tuner need the structured shape to validate against.

    Returns a :class:`ReplayReport`. The ``pairs`` list contains
    every event where BOTH the current and variant scoring succeeded.
    Failure counters are also returned so a silently-shrunken replay
    set cannot pass unnoticed.
    """
    events = [e for e in iter_events(conn, limit=sample_size) if e.kind in kind_filter]
    pairs: list[ReplayPair] = []
    failed_current = 0
    failed_variant = 0
    for event in events:
        try:
            out_current = scoring_fn(conn, event, current_params)
        except Exception as e:
            log.warning("replay.scoring_failed", which="current", event_id=event.id, error=str(e))
            failed_current += 1
            continue
        try:
            out_variant = scoring_fn(conn, event, variant_params)
        except Exception as e:
            log.warning("replay.scoring_failed", which="variant", event_id=event.id, error=str(e))
            failed_variant += 1
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
        failed_current=failed_current,
        failed_variant=failed_variant,
        kind_filter=kind_filter,
    )
    return ReplayReport(
        pairs=pairs,
        sample_size_requested=sample_size,
        sample_size_kept=len(pairs),
        failed_current_count=failed_current,
        failed_variant_count=failed_variant,
    )


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
