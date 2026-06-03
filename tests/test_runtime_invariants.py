"""Runtime invariant emission — closes the R1 rollback path.

The RollbackMonitor's R1 condition reads pipeline_events for
``stage='tuner.invariant_violation'`` since the most recent promote.
Before this layer, no production code emitted that stage, so R1 was
a dead path.

These tests verify two things:
  1. The salience worker and mode_switcher each emit
     ``tuner.invariant_violation`` when their own runtime guard
     check fails on a generated output.
  2. The RollbackMonitor's R1 logic correctly correlates such an
     event with a recent promote and fires a rollback.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from afair.agents.rollback_monitor import RollbackMonitor
from afair.agents.salience import SalienceWorker
from afair.substrate import open_db, tuner_state


@pytest.fixture()
def conn(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    c = open_db(vault)
    yield c
    c.close()


# ─── salience runtime guard ───────────────────────────────────────────────


def test_salience_emits_invariant_violation_when_guard_fails(conn) -> None:
    """Force check_salience_outputs to fail and verify the worker
    emits tuner.invariant_violation pipeline_event instead of
    writing the bad interpretation."""
    from afair.settings import Settings
    from afair.substrate.events import write_event_with_status

    # Seed one event so the salience worker has work to do.
    write_event_with_status(
        conn,
        kind="remember",
        origin="agent",
        payload={"content_type": "text", "text": "hello world"},
    )

    # Stub check_salience_outputs to return a failed GuardResult.
    from afair.agents.guards import GuardResult

    bad = GuardResult(passed=False, failures=("forced failure for test",), sample_count=1)
    with patch("afair.agents.salience.check_salience_outputs", return_value=bad):
        stats = SalienceWorker().run(conn, Settings())

    assert stats["invariant_violations"] >= 1
    assert stats["scored"] == 0  # no interpretation row written

    # The pipeline_event was emitted with the right stage + detail.
    row = conn.execute(
        """
        SELECT stage, producer, detail FROM pipeline_events
        WHERE stage = 'tuner.invariant_violation'
        ORDER BY recorded_at DESC LIMIT 1
        """,
    ).fetchone()
    assert row is not None
    assert "salience" in row["detail"]
    assert "component_weights" in row["detail"]


def test_salience_passes_guards_under_normal_operation(conn) -> None:
    """The clamp logic in score_event guarantees [0, 1] + full
    component dict. Under normal operation the guard PASSES and the
    worker writes the interpretation as before."""
    from afair.settings import Settings
    from afair.substrate.events import write_event_with_status

    write_event_with_status(
        conn,
        kind="remember",
        origin="agent",
        payload={"content_type": "text", "text": "hello world"},
    )

    stats = SalienceWorker().run(conn, Settings())

    assert stats["scored"] >= 1
    assert stats.get("invariant_violations", 0) == 0
    # No invariant_violation pipeline_event recorded.
    row = conn.execute(
        """
        SELECT 1 FROM pipeline_events
        WHERE stage = 'tuner.invariant_violation'
        LIMIT 1
        """,
    ).fetchone()
    assert row is None


# ─── mode_switcher runtime guard ──────────────────────────────────────────


def test_mode_switcher_emits_invariant_violation_on_invalid_mode(conn) -> None:
    """Patch _decide_target_mode to return garbage; the worker's
    runtime guard catches it and emits the invariant event."""
    from afair.agents.mode_switcher import ModeSwitcher

    # Seed enough salience-scored events that the worker has data.
    from afair.agents.salience import SALIENCE_PRODUCED_BY, SalienceWorker
    from afair.settings import Settings
    from afair.substrate.events import write_event_with_status

    for i in range(20):
        write_event_with_status(
            conn,
            kind="remember",
            origin="agent",
            payload={"content_type": "text", "text": f"e{i}"},
        )
    SalienceWorker().run(conn, Settings())
    _ = SALIENCE_PRODUCED_BY  # silence unused warning

    with patch(
        "afair.agents.mode_switcher._decide_target_mode",
        return_value="INVALID_MODE_VALUE",
    ):
        stats = ModeSwitcher().run(conn, Settings())

    assert stats.get("invariant_violation") is True

    row = conn.execute(
        """
        SELECT stage, detail FROM pipeline_events
        WHERE stage = 'tuner.invariant_violation' AND detail LIKE '%mode_switcher%'
        LIMIT 1
        """,
    ).fetchone()
    assert row is not None


# ─── rollback monitor R1: invariant violation → rollback ──────────────────


def test_rollback_monitor_R1_fires_on_invariant_event(conn) -> None:
    """When a tuner.invariant_violation event lands AFTER a promote,
    the monitor rolls the promote back immediately. No need to wait
    for the 50-event window — invariant signal is hard."""
    from afair.settings import Settings

    # Plant a promote 2 hours ago on salience.component_weights.
    promote_ts = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    old_weights = {
        "entity_density": 0.25,
        "link_density": 0.20,
        "has_conflict": 0.10,
        "type_hint_bump": 0.15,
        "is_compound": 0.10,
        "recency": 0.20,
    }
    new_weights = {
        "entity_density": 0.30,
        "link_density": 0.18,
        "has_conflict": 0.10,
        "type_hint_bump": 0.14,
        "is_compound": 0.10,
        "recency": 0.18,
    }
    with conn:
        conn.execute(
            """
            INSERT INTO tuner_state (
                id, recorded_at, kind, worker, tunable,
                old_value_json, new_value_json, evidence_json, rationale
            ) VALUES (?, ?, 'promote', 'salience', 'component_weights', ?, ?, ?, 'test')
            """,
            (
                f"p_{time.time_ns()}",
                promote_ts,
                json.dumps(old_weights),
                json.dumps(new_weights),
                json.dumps(
                    {
                        "pre_promote_baseline": {
                            "useful_count": 0,
                            "not_useful_count": 0,
                            "sample_rows": 0,
                        }
                    }
                ),
            ),
        )

    # Plant a tuner.invariant_violation pipeline_event 30 min ago
    # referencing salience.component_weights in the detail.
    violation_ts = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    with conn:
        conn.execute(
            """
            INSERT INTO pipeline_events (
                id, event_id, event_hash, stage, status,
                recorded_at, producer, detail
            ) VALUES (?, ?, NULL, 'tuner.invariant_violation', 'failed',
                      ?, 'salience:v0',
                      'salience.component_weights output failed invariant: forced')
            """,
            (f"pe_{time.time_ns()}", "test_event", violation_ts),
        )

    stats = RollbackMonitor().run(conn, Settings())
    assert stats["rollbacks_fired"] == 1

    rollbacks = [r for r in tuner_state.history(conn, limit=20) if r.kind == "rollback"]
    assert len(rollbacks) == 1
    assert rollbacks[0].new_value == old_weights
    assert "invariant violation" in (rollbacks[0].rationale or "").lower()
