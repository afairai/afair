"""
Rollback monitor — Phase B safety guard.

Independent cold-path worker that watches every promote the tuner
makes and fires a rollback if post-promote signals indicate the
variant degraded production quality.

Why a separate worker (not part of the tuner):
  * Single Responsibility — tuner promotes, monitor rolls back.
  * Different cadence — monitor runs every 5 min to catch
    degradation fast; tuner runs every 10+ min and only when a
    traffic/time trigger fires.
  * No shared lock — monitor's writes don't conflict with the tuner's
    cycle because they target different paths through the same
    append-only table.

Rollback decision per promote (one of these fires):

  R1. Invariant violation in production output. A pipeline_events
      row with stage='tuner.invariant_violation' that arrived after
      a promote → rollback that promote immediately. Hard signal.

  R2. Feedback-signal degradation. Once ≥ ROLLBACK_EVENT_WINDOW new
      events have arrived since a promote, compare post-promote
      useful-rate to the pre-promote baseline stored in the promote's
      evidence_json. If useful drops by ≥ DEGRADATION_THRESHOLD_PCT %,
      rollback.

  R3. Stale promote with no signal. If a promote has been live
      longer than ROLLBACK_STALE_DAYS without enough feedback to
      compute a verdict, the monitor leaves it alone. (Manual review
      can clear it.)

Each rollback writes a tuner_state.kind='rollback' row carrying the
reason in evidence_json and restoring the old_value from the
promote's old_value_json.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from ..substrate import tuner_state
from .cold_path import ColdPathWorker
from .tunable_registry import (
    ChangeRejected,
    TunableRegistry,
    record_change,
)

if TYPE_CHECKING:
    import sqlite3

    from ..settings import Settings


log = structlog.get_logger(__name__)


# Wait until at least this many new events have landed since the
# promote before drawing any conclusion. Lower → faster reaction,
# noisier signal; higher → more confidence, slower.
ROLLBACK_EVENT_WINDOW = 50

# Useful-rate drop (vs the promote-time baseline) that fires rollback.
# 10% per the plan.
DEGRADATION_THRESHOLD_PCT = 10.0

# Stop monitoring a promote after this many days. Avoids the monitor
# re-firing forever on an ancient promote that never accumulated
# enough feedback signal.
ROLLBACK_STALE_DAYS = 14


class RollbackMonitor(ColdPathWorker):
    """Polls active promotes for degradation; rolls back when needed."""

    name = "rollback_monitor"
    # Every 5 minutes — fast enough to react before a bad promote
    # touches too many events, slow enough that it doesn't hammer
    # the substrate with empty work.
    interval_seconds = 5 * 60

    def run(self, conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
        _ = settings
        stats: dict[str, Any] = {
            "promotes_checked": 0,
            "rollbacks_fired": 0,
            "decisions": [],
        }

        for promote in self._active_promotes(conn):
            stats["promotes_checked"] += 1
            decision = self._evaluate(conn, promote)
            stats["decisions"].append(decision)
            if decision["fire_rollback"]:
                self._fire_rollback(conn, promote, decision)
                stats["rollbacks_fired"] += 1

        log.info("rollback_monitor.cycle", **stats)
        return stats

    # ─── candidate selection ──────────────────────────────────────────

    def _active_promotes(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        """All promotes within the staleness window that have NOT yet
        been rolled back on the same (worker, tunable)."""
        cutoff = (datetime.now(UTC) - timedelta(days=ROLLBACK_STALE_DAYS)).isoformat()
        promotes = conn.execute(
            """
            SELECT id, recorded_at, worker, tunable,
                   old_value_json, new_value_json, evidence_json
            FROM tuner_state
            WHERE kind = 'promote' AND recorded_at > ?
            ORDER BY recorded_at DESC
            """,
            (cutoff,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for p in promotes:
            # Skip if a more recent rollback OR promote already supersedes
            # this one for the same (worker, tunable).
            newer = conn.execute(
                """
                SELECT kind FROM tuner_state
                WHERE worker = ? AND tunable = ? AND recorded_at > ?
                  AND kind IN ('promote', 'rollback')
                ORDER BY recorded_at DESC LIMIT 1
                """,
                (p["worker"], p["tunable"], p["recorded_at"]),
            ).fetchone()
            if newer is not None:
                continue
            out.append(dict(p))
        return out

    # ─── per-promote evaluation ───────────────────────────────────────

    def _evaluate(
        self,
        conn: sqlite3.Connection,
        promote: dict[str, Any],
    ) -> dict[str, Any]:
        """Decide whether THIS promote should be rolled back. Returns a
        dict shaped for the decision log row."""
        worker = promote["worker"]
        tunable = promote["tunable"]
        recorded_at = promote["recorded_at"]

        decision: dict[str, Any] = {
            "promote_id": promote["id"],
            "worker": worker,
            "tunable": tunable,
            "fire_rollback": False,
            "reason": None,
        }

        # R1 — invariant violation since the promote? Check pipeline_events
        # for a tuner.invariant_violation stage. (Salience worker writes
        # this stage when its runtime output fails check_salience_outputs.)
        viol = conn.execute(
            """
            SELECT 1 FROM pipeline_events
            WHERE stage = 'tuner.invariant_violation'
              AND recorded_at > ?
              AND (detail LIKE ? OR detail LIKE ?)
            LIMIT 1
            """,
            (recorded_at, f"%{worker}%", f"%{tunable}%"),
        ).fetchone()
        if viol is not None:
            decision["fire_rollback"] = True
            decision["reason"] = "invariant violation in production output"
            return decision

        # R2 — feedback-signal degradation. Need at least
        # ROLLBACK_EVENT_WINDOW events since the promote.
        events_since = self._count_events_since(conn, recorded_at)
        decision["events_since_promote"] = events_since
        if events_since < ROLLBACK_EVENT_WINDOW:
            decision["reason"] = f"only {events_since}/{ROLLBACK_EVENT_WINDOW} events since promote"
            return decision

        # Pre-promote baseline lives in the promote's evidence.
        baseline = self._read_baseline(promote)
        post = self._read_post_promote_signal(conn, recorded_at)
        decision["baseline"] = baseline
        decision["post_promote"] = post

        # If either sample is empty (no feedback signal), we can't
        # conclude — leave the promote in place.
        if baseline["sample_rows"] == 0 or post["sample_rows"] == 0:
            decision["reason"] = "insufficient feedback signal to judge"
            return decision

        baseline_rate = _useful_rate(baseline)
        post_rate = _useful_rate(post)
        decision["baseline_useful_rate"] = round(baseline_rate, 3)
        decision["post_useful_rate"] = round(post_rate, 3)

        # Degradation = drop in useful_rate, as a percentage of baseline.
        # If baseline is 0 we can't compute a ratio; fall back to absolute.
        if baseline_rate > 0:
            drop_pct = (baseline_rate - post_rate) / baseline_rate * 100
        else:
            drop_pct = (1.0 - post_rate) * 100  # fewer useful than baseline 0+
        decision["drop_pct"] = round(drop_pct, 2)

        if drop_pct >= DEGRADATION_THRESHOLD_PCT:
            decision["fire_rollback"] = True
            decision["reason"] = (
                f"useful-rate dropped {drop_pct:.1f}% (baseline {baseline_rate:.2f} "
                f"→ post {post_rate:.2f}); threshold {DEGRADATION_THRESHOLD_PCT}%"
            )
        else:
            decision["reason"] = (
                f"degradation {drop_pct:.1f}% under threshold {DEGRADATION_THRESHOLD_PCT}%"
            )
        return decision

    def _count_events_since(self, conn: sqlite3.Connection, ts_iso: str) -> int:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM events WHERE created_at > ?",
            (ts_iso,),
        ).fetchone()
        return int(row["c"]) if row else 0

    def _read_baseline(self, promote: dict[str, Any]) -> dict[str, int]:
        """Parse the pre_promote_baseline stashed in the promote's evidence."""
        try:
            ev = json.loads(promote["evidence_json"] or "{}")
        except json.JSONDecodeError:
            return {"useful_count": 0, "not_useful_count": 0, "sample_rows": 0}
        b = ev.get("pre_promote_baseline") or {}
        return {
            "useful_count": int(b.get("useful_count", 0)),
            "not_useful_count": int(b.get("not_useful_count", 0)),
            "sample_rows": int(b.get("sample_rows", 0)),
        }

    def _read_post_promote_signal(
        self,
        conn: sqlite3.Connection,
        promote_ts_iso: str,
    ) -> dict[str, int]:
        """Sum feedback observations recorded after the promote."""
        rows = conn.execute(
            """
            SELECT evidence_json FROM tuner_state
            WHERE kind = 'observation'
              AND worker = 'recall'
              AND tunable = 'feedback'
              AND recorded_at > ?
            """,
            (promote_ts_iso,),
        ).fetchall()
        useful_count = 0
        not_useful_count = 0
        for r in rows:
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

    # ─── rollback execution ──────────────────────────────────────────

    def _fire_rollback(
        self,
        conn: sqlite3.Connection,
        promote: dict[str, Any],
        decision: dict[str, Any],
    ) -> None:
        """Write the rollback row, restoring the promote's old_value."""
        try:
            old_value = json.loads(promote["old_value_json"] or "null")
            new_value = json.loads(promote["new_value_json"] or "null")
        except json.JSONDecodeError:
            log.error("rollback_monitor.bad_promote_row", promote_id=promote["id"])
            return

        registry = TunableRegistry(conn)
        try:
            record_change(
                registry,
                kind="rollback",
                worker=promote["worker"],
                tunable=promote["tunable"],
                old_value=new_value,  # current value at the time of rollback
                new_value=old_value,  # value we're restoring
                evidence={
                    "promote_id": promote["id"],
                    "decision": decision,
                },
                rationale=decision.get("reason") or "rollback by monitor",
            )
            log.warning(
                "rollback_monitor.fired",
                worker=promote["worker"],
                tunable=promote["tunable"],
                reason=decision.get("reason"),
            )
        except ChangeRejected as e:
            # If the rollback fails its own validation (e.g.,
            # old_value is out of bounds due to a registry spec
            # change between promote and now), log + skip. The
            # operator notices the failed_rollback row and decides
            # how to proceed.
            tuner_state.write(
                conn,
                kind="observation",
                worker=promote["worker"],
                tunable=promote["tunable"],
                evidence={
                    "rollback_failed": True,
                    "validation_error": str(e),
                    "promote_id": promote["id"],
                },
                rationale=f"rollback rejected: {e}",
            )
            log.error(
                "rollback_monitor.rejected",
                worker=promote["worker"],
                tunable=promote["tunable"],
                error=str(e),
            )


# ─── helpers ─────────────────────────────────────────────────────────────


def _useful_rate(snapshot: dict[str, int]) -> float:
    """useful / (useful + not_useful). 0 if no signal at all."""
    total = snapshot["useful_count"] + snapshot["not_useful_count"]
    if total == 0:
        return 0.0
    return snapshot["useful_count"] / total
