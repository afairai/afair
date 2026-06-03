"""
Self-improvement tuner — Phase B implementation.

Closes the loop from
``analysis/2026-06-03-recursive-self-improvement.md``: generate
hypotheses, run replay + invariant guards + LLM-judge panel, and
PROMOTE the variant when judge majority + guards both pass.

Phase B adds (over Phase A's observe-only behavior):
  * Real LLM-judge integration when a worker has a replay+judge
    shape registered.
  * ``promote_enabled=True`` actually writes promote rows.
  * Cooldown per tunable: after a rollback, the tunable is locked
    for ``ROLLBACK_COOLDOWN_DAYS``.
  * Halt conditions: if more than ``MAX_ROLLBACKS_PER_WEEK`` have
    fired in the last 7 days, all promotion pauses (observation
    rows still get written so we can see what would have been
    proposed).
  * Hypothesis diversity: rotates through tunables that are due
    for an exploration step instead of always probing the same one.
  * Token-budget tracking is delegated to the judge panel
    (``judge_pairs`` aborts mid-run if its per-cycle budget
    exhausts).

What this writes to ``tuner_state``:
  - ``kind='hypothesis'`` per cycle that selects a candidate.
  - ``kind='observation'`` for every cycle (verdict, guards,
    halt status, rejection reasons, judge stats).
  - ``kind='promote'`` ONLY when guards pass AND judge majority for
    the variant ≥ ``PROMOTE_THRESHOLD``. Carries the pre-promote
    baseline so the rollback monitor can detect degradation later.
  - ``kind='rollback'`` is written by ``RollbackMonitor``, not here.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from ..substrate import pipeline_events as pe
from ..substrate import tuner_state
from .cold_path import ColdPathWorker
from .guards import check_salience_outputs
from .llm_judge import (
    DEFAULT_PANEL,
    DEFAULT_PROMOTE_THRESHOLD,
    DEFAULT_TOKEN_BUDGET,
    JudgePair,
    JudgeReport,
    judge_pairs,
)
from .replay import ReplayReport, replay_with_variants
from .salience import score_event
from .tunable_registry import (
    REGISTRY,
    ChangeRejected,
    TunableRegistry,
    record_change,
    validate_change,
)

if TYPE_CHECKING:
    import sqlite3

    from ..settings import Settings


log = structlog.get_logger(__name__)


# ─── triggers + cadence ───────────────────────────────────────────────────


# Traffic trigger — run when ≥ this many new events have arrived
# since the last tuner cycle.
TRAFFIC_TRIGGER_EVENT_COUNT = 50

# Time fallback trigger — also run after this many seconds even if
# traffic threshold not met.
TIME_TRIGGER_SECONDS = 24 * 3600

# Replay sample size per hypothesis. 30 keeps judge token use bounded.
REPLAY_SAMPLE_SIZE = 30

# Promote threshold — variant must win this share of judge pairs.
PROMOTE_THRESHOLD = DEFAULT_PROMOTE_THRESHOLD  # 0.70


# ─── halt conditions ──────────────────────────────────────────────────────


# After a rollback, lock the same tunable for this many days. Stops
# the tuner from immediately re-proposing the same value the rollback
# just reverted, and gives feedback signals time to accumulate.
ROLLBACK_COOLDOWN_DAYS = 7

# Global halt: if more than this many rollbacks fired in a 7-day
# rolling window, pause ALL promotions. Tuner still observes; humans
# decide whether to clear the halt.
MAX_ROLLBACKS_PER_WEEK = 3


def _is_halted(conn: sqlite3.Connection) -> tuple[bool, str | None]:
    """Check global halt conditions. Returns (halted, reason)."""
    week_ago = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    row = conn.execute(
        """
        SELECT COUNT(*) AS c FROM tuner_state
        WHERE kind = 'rollback' AND recorded_at > ?
        """,
        (week_ago,),
    ).fetchone()
    rollback_count = int(row["c"]) if row else 0
    if rollback_count > MAX_ROLLBACKS_PER_WEEK:
        return True, (
            f"{rollback_count} rollbacks in last 7 days exceeds "
            f"MAX_ROLLBACKS_PER_WEEK={MAX_ROLLBACKS_PER_WEEK}"
        )
    return False, None


def _is_in_cooldown(conn: sqlite3.Connection, worker: str, tunable: str) -> bool:
    """True if this tunable had a rollback within ROLLBACK_COOLDOWN_DAYS."""
    last_rb = tuner_state.last_rollback_at(conn, worker=worker, tunable=tunable)
    if last_rb is None:
        return False
    cutoff = datetime.now(UTC) - timedelta(days=ROLLBACK_COOLDOWN_DAYS)
    return datetime.fromisoformat(last_rb) > cutoff


# ─── worker-specific replay + judge plumbing ──────────────────────────────


def _salience_full_output(
    conn: sqlite3.Connection,
    event: Any,
    weights: dict[str, float],
) -> dict[str, Any]:
    """Adapter: wrap salience.score_event into the dict shape the
    invariant guards expect."""
    score, components = score_event(conn, event, weights=weights)
    return {
        "salience": score,
        "salience_components": components,
        "status": "success",
    }


# Per-worker "this is what the worker DOES" string for the judge prompt.
# Frozen alongside the judge prompt version so judge calls are
# reproducible.
WORKER_QUALITY_CRITERIA: dict[str, str] = {
    "salience": (
        "Higher salience means an event is more worth remembering for "
        "future recall. The weighting should reward events with named "
        "entities, with semantic links to other events, that the user "
        "explicitly type-hinted as important (decision/preference/fact/"
        "deadline), that are compound (deliberately bundled), and that "
        "are recent. Output A and B are full salience extractions for "
        "the SAME input event with two different weight vectors. Pick "
        "the one whose salience score better reflects what a thoughtful "
        "person would mark as 'matters'."
    ),
}

WORKER_PURPOSE: dict[str, str] = {
    "salience": "Score each event for how much it should matter to future recall.",
}


def _build_judge_pairs(
    worker: str,
    replay: ReplayReport,
) -> list[JudgePair]:
    """Construct JudgePair objects from a replay's matched outputs.

    Phase B supports only ``salience`` initially. Other workers that
    grow a replay shape later will need their own entry here.
    """
    purpose = WORKER_PURPOSE.get(worker, "")
    criteria = WORKER_QUALITY_CRITERIA.get(worker, "")
    pairs: list[JudgePair] = []
    for p in replay.pairs:
        # Stringify the dict outputs so the judge sees structured JSON
        # in the prompt. Truncated keys so prompt size stays bounded.
        out_a = _stringify_output(p.output_current)
        out_b = _stringify_output(p.output_variant)
        pairs.append(
            JudgePair(
                input_summary=p.input_summary,
                output_a=out_a,
                output_b=out_b,
                worker_name=worker,
                worker_purpose=purpose,
                quality_criteria=criteria,
            ),
        )
    return pairs


def _stringify_output(out: Any) -> str:
    """Render a worker's structured output for the judge prompt.

    Keep it deterministic (sorted keys, fixed precision) so identical
    outputs produce identical prompts across panel members.
    """
    import json

    return json.dumps(out, sort_keys=True, indent=2, default=str)


# ─── tuner worker ─────────────────────────────────────────────────────────


class Tuner(ColdPathWorker):
    """The self-improvement worker.

    Cold-path. Runs after the traffic trigger fires. One hypothesis
    per cycle.

    ``promote_enabled`` defaults to True in Phase B. The flag still
    exists so:
      * Tests can disable promotion explicitly.
      * Operators can fall back to observe-only by setting the flag
        without redeploying the worker.
    """

    name = "tuner"
    interval_seconds = 10 * 60

    def __init__(self, *, promote_enabled: bool = True) -> None:
        self.promote_enabled = promote_enabled

    def run(self, conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
        _ = settings
        stats: dict[str, Any] = {
            "triggered": False,
            "halted": False,
            "halt_reason": None,
            "hypothesis": None,
            "verdict": None,
            "promoted": False,
        }

        if not self._should_run(conn):
            return stats
        stats["triggered"] = True

        # Global halt — check before any work.
        halted, reason = _is_halted(conn)
        if halted:
            stats["halted"] = True
            stats["halt_reason"] = reason
            tuner_state.write(
                conn,
                kind="observation",
                worker="tuner",
                tunable="meta",
                evidence={"halted": True, "reason": reason},
                rationale="tuner halted by safety condition",
            )
            log.warning("tuner.halted", reason=reason)
            return stats

        # Pick a hypothesis respecting cooldowns + diversity.
        registry = TunableRegistry(conn)
        hypothesis = self._next_hypothesis(conn, registry)
        if hypothesis is None:
            log.info("tuner.no_eligible_hypothesis")
            tuner_state.write(
                conn,
                kind="observation",
                worker="tuner",
                tunable="meta",
                evidence={"reason": "no eligible hypothesis"},
                rationale="all tunables in cooldown or no movement possible",
            )
            return stats

        worker, tunable, current, proposed, rationale = hypothesis
        stats["hypothesis"] = {
            "worker": worker,
            "tunable": tunable,
            "current": current,
            "proposed": proposed,
            "rationale": rationale,
        }

        # Validate the proposed change. Defense in depth — record_change
        # also validates, but failing early gives us a cleaner
        # observation row.
        spec = registry.get_spec(worker, tunable)
        try:
            validate_change(spec=spec, current=current, proposed=proposed)
        except ChangeRejected as e:
            self._write_observation(
                conn,
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

        # Replay + guards.
        replay = self._do_replay(conn, worker, tunable, current, proposed)
        guards_passed = self._check_guards(worker, replay)

        verdict_record: dict[str, Any] = {
            "replay_pair_count": replay.sample_size_kept if replay else 0,
            "replay_failed_count": replay.failed_any_count if replay else 0,
            "guards_passed": guards_passed,
            "judge_panel": "skipped:no_replay_shape" if replay is None else None,
            "promote_attempted": False,
            "promoted": False,
        }

        if not guards_passed:
            verdict_record["judge_panel"] = "skipped:guards_failed"
            self._write_observation(
                conn,
                worker=worker,
                tunable=tunable,
                old_value=current,
                new_value=proposed,
                evidence=verdict_record,
                rationale="invariant guards rejected variant",
            )
            stats["verdict"] = verdict_record
            return stats

        # If we have no replay shape for this tunable, the cycle ends
        # here as a pure observation. The tuner cannot validate a
        # variant it cannot run.
        if replay is None or worker not in WORKER_QUALITY_CRITERIA:
            verdict_record["judge_panel"] = "skipped:no_replay_shape"
            self._write_observation(
                conn,
                worker=worker,
                tunable=tunable,
                old_value=current,
                new_value=proposed,
                evidence=verdict_record,
                rationale="no replay/judge shape registered for this worker — observation only",
            )
            stats["verdict"] = verdict_record
            return stats

        # Judge panel (Phase B).
        if not self.promote_enabled:
            verdict_record["judge_panel"] = "skipped:promote_enabled_false"
            self._write_observation(
                conn,
                worker=worker,
                tunable=tunable,
                old_value=current,
                new_value=proposed,
                evidence=verdict_record,
                rationale="promote_enabled=False — observation only",
            )
            stats["verdict"] = verdict_record
            return stats

        judge_report = self._run_judge_panel(replay, worker)
        verdict_record["judge_panel"] = {
            "panel": list(judge_report.panel),
            "pair_count": judge_report.pair_count,
            "a_wins": judge_report.a_wins,
            "b_wins": judge_report.b_wins,
            "ties": judge_report.ties,
            "b_share": round(judge_report.b_share, 3),
            "tokens_spent": judge_report.tokens_spent_estimate,
            "aborted": judge_report.aborted,
            "abort_reason": judge_report.abort_reason,
        }

        if judge_report.aborted:
            self._write_observation(
                conn,
                worker=worker,
                tunable=tunable,
                old_value=current,
                new_value=proposed,
                evidence=verdict_record,
                rationale=f"judge panel aborted: {judge_report.abort_reason}",
            )
            stats["verdict"] = verdict_record
            return stats

        # Promotion decision.
        verdict_record["promote_attempted"] = True
        promoted = judge_report.b_share >= PROMOTE_THRESHOLD

        if not promoted:
            self._write_observation(
                conn,
                worker=worker,
                tunable=tunable,
                old_value=current,
                new_value=proposed,
                evidence=verdict_record,
                rationale=(
                    f"variant b_share={judge_report.b_share:.2f} "
                    f"below promote threshold {PROMOTE_THRESHOLD}"
                ),
            )
            stats["verdict"] = verdict_record
            return stats

        # Promote. Stash a pre-promote baseline in evidence so the
        # rollback monitor can detect post-promote degradation.
        baseline = _read_feedback_baseline(conn, lookback_events=50)
        promote_evidence: dict[str, Any] = {
            "judge_panel": verdict_record["judge_panel"],
            "replay_pair_count": verdict_record["replay_pair_count"],
            "guards_passed": True,
            "pre_promote_baseline": baseline,
            "judge_prompt_version": "v0:2026-06-03",
        }
        try:
            record_change(
                registry,
                kind="promote",
                worker=worker,
                tunable=tunable,
                old_value=current,
                new_value=proposed,
                evidence=promote_evidence,
                rationale=(
                    f"variant won {judge_report.b_share:.0%} of judge pairs "
                    f"(threshold {PROMOTE_THRESHOLD:.0%})"
                ),
            )
            verdict_record["promoted"] = True
            stats["promoted"] = True
            log.info(
                "tuner.promoted",
                worker=worker,
                tunable=tunable,
                old=current,
                new=proposed,
                b_share=judge_report.b_share,
            )
        except ChangeRejected as e:
            # Final guard: record_change re-validates and may still
            # refuse (e.g., cross-tunable invariant).
            verdict_record["promote_attempted"] = True
            verdict_record["promote_rejected_reason"] = str(e)
            self._write_observation(
                conn,
                worker=worker,
                tunable=tunable,
                old_value=current,
                new_value=proposed,
                evidence=verdict_record,
                rationale=f"record_change rejected promote: {e}",
            )
            stats["verdict"] = verdict_record
            return stats

        # Mirror to pipeline_events for the observability surface.
        pe.record(
            conn,
            event_id=f"tuner:{worker}:{tunable}",
            stage="tuner.promoted",
            producer="tuner:v1",
            detail=f"{current} -> {proposed} (b_share={judge_report.b_share:.2f})",
        )
        stats["verdict"] = verdict_record
        return stats

    # ─── triggers ─────────────────────────────────────────────────────

    def _should_run(self, conn: sqlite3.Connection) -> bool:
        """Run if traffic OR time threshold met."""
        last = self._last_cycle_at(conn)
        if last is None:
            return False
        if time.time() - last >= TIME_TRIGGER_SECONDS:
            return True
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM events WHERE created_at > ?",
            (self._iso_from_epoch(last),),
        ).fetchone()
        events_since = row["c"] if row else 0
        return events_since >= TRAFFIC_TRIGGER_EVENT_COUNT

    def _last_cycle_at(self, conn: sqlite3.Connection) -> float | None:
        row = conn.execute(
            """
            SELECT recorded_at FROM tuner_state
            WHERE kind IN ('observation', 'promote', 'rollback')
            ORDER BY recorded_at DESC LIMIT 1
            """,
        ).fetchone()
        if row is None:
            return time.time()
        return datetime.fromisoformat(row["recorded_at"]).timestamp()

    @staticmethod
    def _iso_from_epoch(epoch: float) -> str:
        return datetime.fromtimestamp(epoch, tz=UTC).isoformat()

    # ─── hypothesis diversity ─────────────────────────────────────────

    def _next_hypothesis(
        self,
        conn: sqlite3.Connection,
        registry: TunableRegistry,
    ) -> tuple[str, str, Any, Any, str] | None:
        """Pick ONE candidate per cycle. Phase B logic:

        1. Iterate the whitelist in a rotation, preferring tunables
           that have NOT had a recent hypothesis written.
        2. Skip tunables in rollback cooldown.
        3. Alternate direction (up vs down) per tunable based on the
           most recent hypothesis for it.
        4. Return None if nothing eligible remains.
        """
        # Most-recent hypothesis per tunable — drives rotation + direction.
        last_hyp_by_key = self._latest_hypothesis_per_key(conn)
        # Sort tunables by oldest-first hypothesis age (NULL first).
        ordered: list[tuple[str, str]] = []
        for spec in REGISTRY:
            key = (spec.worker, spec.tunable)
            ordered.append(key)
        ordered.sort(
            key=lambda k: last_hyp_by_key.get(k, ""),  # empty string sorts first
        )

        for worker, tunable in ordered:
            if _is_in_cooldown(conn, worker, tunable):
                continue
            spec = registry.get_spec(worker, tunable)
            current = registry.get(worker, tunable)
            proposed, direction = self._propose_value(
                spec, current, last_hyp_by_key.get((worker, tunable))
            )
            if proposed is None:
                continue
            rationale = (
                f"phase-B scout: explore {direction} from {current!r} to {proposed!r} "
                f"(bounds=[{spec.min_value}, {spec.max_value}])"
            )
            return worker, tunable, current, proposed, rationale
        return None

    def _latest_hypothesis_per_key(
        self,
        conn: sqlite3.Connection,
    ) -> dict[tuple[str, str], str]:
        """Map (worker, tunable) → recorded_at of the most recent
        hypothesis row. Used to drive diversity rotation."""
        rows = conn.execute(
            """
            SELECT worker, tunable, MAX(recorded_at) AS latest
            FROM tuner_state
            WHERE kind = 'hypothesis'
            GROUP BY worker, tunable
            """,
        ).fetchall()
        return {(r["worker"], r["tunable"]): r["latest"] for r in rows}

    def _propose_value(
        self,
        spec: Any,
        current: Any,
        last_hypothesis_recorded_at: str | None,
    ) -> tuple[Any, str]:
        """Propose a value within bounded_delta. Alternate direction
        across cycles for the same tunable.

        Returns (proposed_value, direction). Proposed is None when no
        movement is possible (already at min and max, scalar tunable).
        """
        # Direction toggling: simple heuristic — if there's a prior
        # hypothesis on this tunable, pick the OPPOSITE direction this
        # time. We don't track outcomes yet — that's a Phase-C optimization.
        direction_up = (
            last_hypothesis_recorded_at is None
            or hash(
                last_hypothesis_recorded_at,
            )
            % 2
            == 0
        )

        if spec.kind == "float":
            step = float(spec.bounded_delta) * 0.95  # stay safely inside the cap
            if direction_up:
                proposed = float(current) * (1 + step)
                if spec.max_value is not None and proposed > spec.max_value:
                    proposed = float(current) * (1 - step)
                    direction = "down"
                else:
                    direction = "up"
            else:
                proposed = float(current) * (1 - step)
                if spec.min_value is not None and proposed < spec.min_value:
                    proposed = float(current) * (1 + step)
                    direction = "up"
                else:
                    direction = "down"
            # Clip to bounds defensively.
            if spec.max_value is not None:
                proposed = min(proposed, spec.max_value)
            if spec.min_value is not None:
                proposed = max(proposed, spec.min_value)
            proposed = round(proposed, 4)
            return proposed, direction

        if spec.kind == "int":
            step = float(spec.bounded_delta) * 0.95
            if direction_up:
                candidate = round(int(current) * (1 + step))
                if spec.max_value is not None and candidate > spec.max_value:
                    candidate = round(int(current) * (1 - step))
                    direction = "down"
                else:
                    direction = "up"
            else:
                candidate = round(int(current) * (1 - step))
                if spec.min_value is not None and candidate < spec.min_value:
                    candidate = round(int(current) * (1 + step))
                    direction = "up"
                else:
                    direction = "down"
            if candidate == int(current):
                return None, ""
            if spec.max_value is not None:
                candidate = min(candidate, spec.max_value)
            if spec.min_value is not None:
                candidate = max(candidate, spec.min_value)
            return int(candidate), direction

        if spec.kind == "weights_dict":
            # Shift ONE component by step%, balance the rest so the
            # sum stays 1.0. Pick the component that has the LEAST
            # recently been the focus — for now, just the first key.
            current_d = dict(current) if isinstance(current, dict) else {}
            keys = sorted(current_d.keys())
            if not keys:
                # Corrupted / missing registry value — skip this tunable.
                return None, ""
            focus = keys[0]
            step = float(spec.bounded_delta) * 0.5  # half the cap; weights are touchy
            direction = "up" if direction_up else "down"
            delta = current_d[focus] * step * (1 if direction_up else -1)
            current_d[focus] = round(current_d[focus] + delta, 4)
            # Re-balance: distribute the negative of delta across the rest.
            others = [k for k in keys if k != focus]
            spread = -delta / len(others)
            for k in others:
                current_d[k] = round(current_d[k] + spread, 4)
            # Clip each to bounds.
            if spec.min_value is not None:
                for k in current_d:
                    current_d[k] = max(current_d[k], spec.min_value)
            if spec.max_value is not None:
                for k in current_d:
                    current_d[k] = min(current_d[k], spec.max_value)
            # Normalize to exactly 1.0 after clipping.
            total = sum(current_d.values())
            if total > 0:
                current_d = {k: round(v / total, 4) for k, v in current_d.items()}
            return current_d, direction

        if spec.kind == "string":
            # Not auto-tunable yet — prompt-variant pools land in a
            # later phase.
            return None, ""

        return None, ""

    # ─── replay + guards ──────────────────────────────────────────────

    def _do_replay(
        self,
        conn: sqlite3.Connection,
        worker: str,
        tunable: str,
        current: Any,
        proposed: Any,
    ) -> ReplayReport | None:
        if worker == "salience" and tunable == "component_weights":
            return replay_with_variants(
                conn,
                scoring_fn=lambda c, e, p: _salience_full_output(c, e, p["weights"]),
                current_params={"weights": current},
                variant_params={"weights": proposed},
                sample_size=REPLAY_SAMPLE_SIZE,
            )
        return None

    def _check_guards(
        self,
        worker: str,
        replay: ReplayReport | None,
    ) -> bool:
        if replay is None or not replay.pairs:
            return True
        if worker == "salience":
            current_outputs = [p.output_current for p in replay.pairs]
            variant_outputs = [p.output_variant for p in replay.pairs]
            return bool(check_salience_outputs(current_outputs)) and bool(
                check_salience_outputs(variant_outputs),
            )
        return True

    # ─── judge panel ──────────────────────────────────────────────────

    def _run_judge_panel(
        self,
        replay: ReplayReport,
        worker: str,
        *,
        panel: tuple[str, ...] = DEFAULT_PANEL,
        token_budget: int = DEFAULT_TOKEN_BUDGET,
    ) -> JudgeReport:
        """Build judge pairs from the replay and run the multi-vendor
        panel. Aborts gracefully if the panel hits its budget."""
        judge_input = _build_judge_pairs(worker, replay)
        return judge_pairs(judge_input, panel=panel, token_budget=token_budget)

    # ─── helpers ──────────────────────────────────────────────────────

    def _write_observation(
        self,
        conn: sqlite3.Connection,
        *,
        worker: str,
        tunable: str,
        old_value: Any,
        new_value: Any,
        evidence: dict[str, Any],
        rationale: str,
    ) -> None:
        """One-line observation writer — keeps the run() flow readable.

        Also mirrors to pipeline_events so the lifecycle dashboard sees
        every tuner cycle ending.
        """
        tuner_state.write(
            conn,
            kind="observation",
            worker=worker,
            tunable=tunable,
            old_value=old_value,
            new_value=new_value,
            evidence=evidence,
            rationale=rationale,
        )
        pe.record(
            conn,
            event_id=f"tuner:{worker}:{tunable}",
            stage="tuner.cycle_completed",
            producer="tuner:v1",
            detail=rationale[:200],
        )


# ─── pre-promote baseline (used by rollback monitor) ──────────────────────


def _read_feedback_baseline(
    conn: sqlite3.Connection,
    *,
    lookback_events: int = 50,
) -> dict[str, int]:
    """Snapshot the recent recall.feedback signal at promote time.

    The rollback monitor compares the post-promote useful-rate to
    this baseline. Sparse signal is fine — the monitor only fires
    when the post-promote signal CLEARLY degrades; absence of signal
    leaves the promote in place.
    """
    rows = conn.execute(
        """
        SELECT evidence_json FROM tuner_state
        WHERE kind = 'observation'
          AND worker = 'recall'
          AND tunable = 'feedback'
        ORDER BY recorded_at DESC
        LIMIT ?
        """,
        (lookback_events,),
    ).fetchall()
    useful_count = 0
    not_useful_count = 0
    for r in rows:
        import json

        try:
            ev = json.loads(r["evidence_json"]) if r["evidence_json"] else {}
        except json.JSONDecodeError:
            continue
        useful_count += len(ev.get("useful_event_ids", []) or [])
        not_useful_count += len(ev.get("not_useful_event_ids", []) or [])
    return {
        "useful_count": useful_count,
        "not_useful_count": not_useful_count,
        "sample_rows": len(rows),
    }
