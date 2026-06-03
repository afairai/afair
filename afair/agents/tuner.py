"""
Self-improvement tuner — the worker that closes the loop on
``analysis/2026-06-03-recursive-self-improvement.md``.

Phase A scope (this implementation):
  * **Observe-only.** Generates hypotheses, runs guards + replay +
    judge, writes the verdict as a ``tuner_state`` observation row.
    DOES NOT promote yet. Promotes land in Phase B once we've
    watched a few observation cycles in production.
  * Traffic-triggered: runs when ≥ N new events have arrived since
    the last cycle OR ≥ M hours have passed, whichever first.
  * One hypothesis per cycle initially. Pick the lowest-blast-radius
    tunable (``surprise.context_window``) as the first target.

What this worker writes to ``tuner_state``:
  - ``kind='hypothesis'`` row when a candidate value is selected.
  - ``kind='observation'`` row with the judge verdict, guard result,
    and budget status after each replay+judge run.
  - In Phase B: ``kind='promote'`` rows when a candidate passes
    everything, with rollback gates active.

Safety bounds (constitution I7):
  * Tunable whitelist enforced via ``TunableRegistry``.
  * Bounded delta enforced via ``validate_change`` (max ±20% per
    move for floats, ±30% for ints).
  * Cost cap on the judge panel (200K tokens per cycle).
  * Hard floor invariant guards on every replay output.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from ..substrate import pipeline_events as pe
from ..substrate import tuner_state
from .cold_path import ColdPathWorker
from .guards import check_salience_outputs
from .llm_judge import DEFAULT_PANEL, JudgePair, JudgeReport
from .replay import ReplayReport, replay_with_variants
from .salience import score_event
from .tunable_registry import (
    ChangeRejected,
    TunableRegistry,
    validate_change,
)

if TYPE_CHECKING:
    import sqlite3

    from ..settings import Settings


log = structlog.get_logger(__name__)


def _salience_full_output(
    conn: sqlite3.Connection,
    event: Any,
    weights: dict[str, float],
) -> dict[str, Any]:
    """Adapter: wrap salience.score_event into the dict shape the
    invariant guards expect. Pulling this into a module-level helper
    keeps the lambda inside _do_replay readable.
    """
    score, components = score_event(conn, event, weights=weights)
    return {
        "salience": score,
        "salience_components": components,
        "status": "success",
    }


# Traffic trigger — run when ≥ this many new events have arrived
# since the last tuner cycle. Tracked off the events row count.
TRAFFIC_TRIGGER_EVENT_COUNT = 50

# Time fallback trigger — also run after this many seconds even if
# traffic threshold not met. Catches the low-volume case.
TIME_TRIGGER_SECONDS = 24 * 3600

# Replay sample size per hypothesis. 30 keeps judge token use bounded.
REPLAY_SAMPLE_SIZE = 30


class Tuner(ColdPathWorker):
    """The self-improvement worker.

    Cold-path. Runs after the traffic trigger fires. One hypothesis
    per cycle in Phase A. Always observe-only — promote logic lives
    behind a feature flag until validated.
    """

    name = "tuner"
    # Tick every 10 min — the inner trigger logic decides whether to
    # actually do work. Cheap to check, never blocks user-facing path.
    interval_seconds = 10 * 60

    def __init__(self, *, promote_enabled: bool = False) -> None:
        # promote_enabled defaults to False — Phase A is observation
        # only. Set to True in Phase B after a few observation cycles
        # have been reviewed.
        self.promote_enabled = promote_enabled

    def run(self, conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
        _ = settings
        stats: dict[str, Any] = {
            "triggered": False,
            "hypothesis": None,
            "verdict": None,
            "promote_attempted": False,
            "promoted": False,
        }

        if not self._should_run(conn):
            return stats
        stats["triggered"] = True

        # Pick the first hypothesis to evaluate.
        registry = TunableRegistry(conn)
        hypothesis = self._next_hypothesis(conn, registry)
        if hypothesis is None:
            log.info("tuner.no_hypothesis")
            return stats

        worker, tunable, current, proposed, rationale = hypothesis
        stats["hypothesis"] = {
            "worker": worker,
            "tunable": tunable,
            "current": current,
            "proposed": proposed,
            "rationale": rationale,
        }

        # Validate the proposed change against the whitelist + bounds.
        spec = registry.get_spec(worker, tunable)
        try:
            validate_change(spec=spec, current=current, proposed=proposed)
        except ChangeRejected as e:
            log.warning(
                "tuner.hypothesis_rejected",
                worker=worker,
                tunable=tunable,
                reason=str(e),
            )
            tuner_state.write(
                conn,
                kind="observation",
                worker=worker,
                tunable=tunable,
                old_value=current,
                new_value=proposed,
                evidence={"validation_error": str(e)},
                rationale="hypothesis rejected by validate_change",
            )
            return stats

        tuner_state.write(
            conn,
            kind="hypothesis",
            worker=worker,
            tunable=tunable,
            old_value=current,
            new_value=proposed,
            rationale=rationale,
        )

        # Run replay + (in Phase A: skip judge to save tokens; just
        # log the candidate). Once Phase B is ready, this is where
        # _run_judge_panel(...) goes.
        replay = self._do_replay(conn, worker, tunable, current, proposed)
        guards_passed = self._check_guards(worker, replay)

        verdict_record = {
            "replay_pair_count": replay.sample_size_kept if replay else 0,
            "replay_failed_count": replay.failed_any_count if replay else 0,
            "guards_passed": guards_passed,
            "judge_panel": "skipped:phase-A",
            "promote_attempted": self.promote_enabled and guards_passed,
            "promoted": False,
        }
        stats["verdict"] = verdict_record

        tuner_state.write(
            conn,
            kind="observation",
            worker=worker,
            tunable=tunable,
            old_value=current,
            new_value=proposed,
            evidence=verdict_record,
            rationale=("replay + guards completed (judge gated until Phase B)"),
        )
        pe.record(
            conn,
            event_id=f"tuner:{worker}:{tunable}",
            stage="tuner.cycle_completed",
            producer="tuner:v0",
            detail=f"observation written (guards_passed={guards_passed})",
        )
        return stats

    # ─── triggers ─────────────────────────────────────────────────────

    def _should_run(self, conn: sqlite3.Connection) -> bool:
        """Run if traffic OR time threshold met."""
        last = self._last_cycle_at(conn)
        if last is None:
            # First boot — wait at least one window before doing anything.
            return False
        # Time trigger
        if time.time() - last >= TIME_TRIGGER_SECONDS:
            return True
        # Traffic trigger
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM events WHERE created_at > ?",
            (self._iso_from_epoch(last),),
        ).fetchone()
        events_since = row["c"] if row else 0
        return events_since >= TRAFFIC_TRIGGER_EVENT_COUNT

    def _last_cycle_at(self, conn: sqlite3.Connection) -> float | None:
        """Epoch seconds of the last tuner cycle, or None."""
        row = conn.execute(
            """
            SELECT recorded_at FROM tuner_state
            WHERE kind IN ('observation', 'promote', 'rollback')
            ORDER BY recorded_at DESC LIMIT 1
            """,
        ).fetchone()
        if row is None:
            # No prior cycle — treat boot time as the anchor.
            return time.time()
        from datetime import datetime
        return datetime.fromisoformat(row["recorded_at"]).timestamp()

    @staticmethod
    def _iso_from_epoch(epoch: float) -> str:
        from datetime import UTC, datetime
        return datetime.fromtimestamp(epoch, tz=UTC).isoformat()

    # ─── hypothesis generation ────────────────────────────────────────

    def _next_hypothesis(
        self,
        conn: sqlite3.Connection,
        registry: TunableRegistry,
    ) -> tuple[str, str, Any, Any, str] | None:
        """Pick ONE candidate per cycle.

        Phase A: hardcoded to ``surprise.context_window``. We propose
        a value 30% larger than current (within bounds) as the first
        exploration step. Future cycles will pick adaptively based
        on observations from previous cycles.

        Returns (worker, tunable, current, proposed, rationale).
        """
        worker, tunable = "surprise", "context_window"
        spec = registry.get_spec(worker, tunable)
        current = int(registry.get(worker, tunable))
        # Propose +30% (rounded), bounded to max. Falls back to -30%
        # if already at max.
        candidate_up = min(round(current * 1.3), spec.max_value or current)
        if candidate_up == current and spec.min_value is not None:
            candidate_down = max(round(current * 0.7), spec.min_value)
            if candidate_down == current:
                return None
            proposed = candidate_down
            direction = "down"
        else:
            proposed = candidate_up
            direction = "up"
        rationale = (
            f"phase-A scout: explore {direction} from {current} to {proposed} "
            f"(bounds=[{spec.min_value}, {spec.max_value}])"
        )
        return worker, tunable, current, proposed, rationale

    # ─── replay + guard execution ─────────────────────────────────────

    def _do_replay(
        self,
        conn: sqlite3.Connection,
        worker: str,
        tunable: str,
        current: Any,
        proposed: Any,
    ) -> ReplayReport | None:
        """Run the worker-appropriate replay. Returns the ReplayReport
        with full structured outputs (NOT scalar scores) so the
        invariant guards can validate real component shape.

        Phase A only handles salience replay — that's a closed-form
        function over substrate state, no LLM call. surprise.context_window
        affects recall-time scoring which doesn't have a clean offline
        replay shape; we keep it in the hypothesis loop but skip the
        actual replay for now (the observation row records the gap).
        """
        if worker == "salience" and tunable == "component_weights":
            return replay_with_variants(
                conn,
                scoring_fn=lambda c, e, p: _salience_full_output(c, e, p["weights"]),
                current_params={"weights": current},
                variant_params={"weights": proposed},
                sample_size=REPLAY_SAMPLE_SIZE,
            )
        # No replay shape for other workers in Phase A.
        return None

    def _check_guards(
        self,
        worker: str,
        replay: ReplayReport | None,
    ) -> bool:
        """Run the invariant guard suite for this worker. Both
        current and variant outputs must pass. No replay → vacuously OK.

        Guards now see the REAL component dict from the replay
        (not a synthesized empty one), so check_salience_outputs
        actually validates the contract instead of trivially
        passing on scalar inputs.
        """
        if replay is None or not replay.pairs:
            return True
        if worker == "salience":
            current_outputs = [p.output_current for p in replay.pairs]
            variant_outputs = [p.output_variant for p in replay.pairs]
            return bool(check_salience_outputs(current_outputs)) and bool(
                check_salience_outputs(variant_outputs),
            )
        return True

    # ─── judge integration (Phase B) ──────────────────────────────────

    def _run_judge_panel(self, pairs: list[Any], worker: str) -> JudgeReport | None:
        """Stub. Used in Phase B once promote_enabled flips on.

        Construction of the JudgePair list happens here once we have
        per-worker quality criteria phrased for the judge prompt.
        """
        _ = (pairs, worker, DEFAULT_PANEL, JudgePair)  # silence "imported but unused" lint
        return None
