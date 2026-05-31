"""Mode-switching agent — Phase 4 Track 2 final piece.

Tracks the cumulative salience signal across recent events and emits
an ``observe`` event whenever attention should shift between two
cognitive modes:

  * **CEN — Central Executive Network.** Focused, deliberate processing.
    The vault is in CEN mode when recent events carry high salience or
    high surprise — there's a sustained signal that "something matters
    right now." In CEN mode, the consolidator runs less aggressively
    (don't roll up live attention into summaries yet) and recall
    prefers explicit hits over wandering associations.

  * **DMN — Default Mode Network.** Wandering, integrative processing.
    The vault is in DMN mode when recent events are low-salience or
    quiet — the system has bandwidth to fold experience into structure.
    Consolidator runs freely; canonicalizer + bind agents take their
    time; recall surfaces stale-but-relevant memories.

Transition rule
===============
A simple two-threshold scheme to prevent flapping:

  * If cumulative salience over the last N events ≥ ``SWITCH_TO_CEN_THRESHOLD``
    AND we're not already in CEN → switch to CEN, emit
    ``observe(action="mode_switched", subject="cen", ...)``.

  * If cumulative salience over the last N events ≤ ``SWITCH_TO_DMN_THRESHOLD``
    AND we're not already in DMN → switch to DMN, emit
    ``observe(action="mode_switched", subject="dmn", ...)``.

Hysteresis (the gap between thresholds) keeps the mode stable around
the boundary.

The current mode is derived from substrate at any time — the most
recent ``observe(action="mode_switched")`` event's ``subject`` is
the active mode. Defaults to DMN at boot.

Why an observe event (not a separate table)?
============================================
Mode transitions ARE user-observable events. They live in the same
event log as everything else, get extracted, get embedded, get
recalled — "what mode was I in last Tuesday?" is a natural recall
query. Adding a separate ``mode_state`` table would split this
naturally-substrate-shaped data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from ..substrate import pipeline_events as pe
from ..substrate import write_event
from .cold_path import ColdPathWorker
from .salience import read_recent_salience

if TYPE_CHECKING:
    import sqlite3

    from ..settings import Settings

log = structlog.get_logger(__name__)


MODE_SWITCH_KIND = "observe"
MODE_SWITCH_ACTION = "mode_switched"
MODE_CEN = "cen"
MODE_DMN = "dmn"

MODE_SWITCHER_ORIGIN = "agent:mode_switcher"


# Sliding window — how many recent salience-scored events to sum over.
# 20 is small enough to react within minutes, large enough to filter
# out single-event spikes.
SALIENCE_WINDOW_SIZE = 20

# Hysteresis thresholds. Cumulative salience ranges 0..N (where N is
# the window size, assuming max salience=1.0 per event). With N=20:
#   ≥ 8.0 → switch to CEN (avg salience ≥ 0.4 → consistent signal)
#   ≤ 4.0 → switch to DMN (avg salience ≤ 0.2 → quiet)
# The gap (4.0..8.0) is the hysteresis dead-zone.
SWITCH_TO_CEN_THRESHOLD = 8.0
SWITCH_TO_DMN_THRESHOLD = 4.0


class ModeSwitcher(ColdPathWorker):
    """Periodically check whether attention should shift between
    cognitive modes and emit an observe event when it does.

    Triggers off the salience signal that :class:`SalienceWorker`
    produces. Skips quietly when no salience interpretations exist
    yet (early-boot or single-event vault).
    """

    name = "mode_switcher"
    interval_seconds = 120  # every 2 min — reactive but not chatty

    def run(self, conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
        _ = settings
        stats: dict[str, Any] = {
            "current_mode": None,
            "cumulative_salience": 0.0,
            "transitioned": False,
            "to_mode": None,
        }

        recent = read_recent_salience(conn, limit=SALIENCE_WINDOW_SIZE)
        if not recent:
            log.info("mode_switcher.skipped_no_salience")
            return stats

        cumulative = sum(score for _, score, _ in recent)
        stats["cumulative_salience"] = round(cumulative, 3)

        current = read_current_mode(conn)
        stats["current_mode"] = current

        target = _decide_target_mode(current, cumulative)
        if target == current:
            log.info(
                "mode_switcher.no_change",
                current=current,
                cumulative_salience=stats["cumulative_salience"],
            )
            return stats

        _write_mode_transition(
            conn,
            to_mode=target,
            from_mode=current,
            cumulative_salience=cumulative,
            window=SALIENCE_WINDOW_SIZE,
        )
        stats["transitioned"] = True
        stats["to_mode"] = target
        log.info(
            "mode_switcher.transitioned",
            from_mode=current,
            to_mode=target,
            cumulative_salience=stats["cumulative_salience"],
        )
        return stats


def read_current_mode(conn: sqlite3.Connection) -> str:
    """Resolve the current attention mode from substrate.

    The most recent ``observe(action="mode_switched")`` event's
    ``subject`` IS the current mode. Defaults to DMN when no
    mode-transition events exist (clean-vault state).
    """
    import json

    row = conn.execute(
        """
        SELECT payload FROM events
        WHERE kind = ? AND origin = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (MODE_SWITCH_KIND, MODE_SWITCHER_ORIGIN),
    ).fetchone()
    if row is None:
        return MODE_DMN
    try:
        payload = json.loads(row["payload"])
    except (ValueError, TypeError):
        return MODE_DMN
    subject = payload.get("subject")
    if subject == MODE_CEN:
        return MODE_CEN
    if subject == MODE_DMN:
        return MODE_DMN
    return MODE_DMN


def _decide_target_mode(current: str, cumulative: float) -> str:
    """Two-threshold transition decision (hysteresis).

    Returns the mode we SHOULD be in given the current mode + the
    cumulative-salience reading. Same as ``current`` when no
    transition should fire.
    """
    if current != MODE_CEN and cumulative >= SWITCH_TO_CEN_THRESHOLD:
        return MODE_CEN
    if current != MODE_DMN and cumulative <= SWITCH_TO_DMN_THRESHOLD:
        return MODE_DMN
    return current


def _write_mode_transition(
    conn: sqlite3.Connection,
    *,
    to_mode: str,
    from_mode: str,
    cumulative_salience: float,
    window: int,
) -> None:
    """Emit the mode-switched observe event into the substrate.

    Uses the same ``observe`` event shape any agent would. Origin
    ``agent:mode_switcher`` distinguishes it from user-driven
    observations in recall.
    """
    payload = {
        "content_type": "event",
        "action": MODE_SWITCH_ACTION,
        "subject": to_mode,
        "result": f"transition from {from_mode}",
        "cumulative_salience": round(cumulative_salience, 3),
        "window_size": window,
    }
    event = write_event(
        conn,
        origin=MODE_SWITCHER_ORIGIN,
        kind=MODE_SWITCH_KIND,
        payload=payload,
    )
    pe.record(
        conn,
        event_id=event.id,
        event_hash=event.content_hash,
        stage="mode_switcher.transitioned",
        producer=f"mode_switcher:{from_mode}->{to_mode}",
        detail=f"cumulative_salience={cumulative_salience:.3f}",
    )
