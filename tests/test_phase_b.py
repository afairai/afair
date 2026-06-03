"""Phase B integration tests — promote / cooldown / halt / rollback monitor.

Mocks the LLM judge panel (no real network calls). Verifies the
full promote → monitor → rollback feedback loop end-to-end on a
substrate fixture.
"""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from afair.agents.llm_judge import JudgeReport, PanelVerdict
from afair.agents.rollback_monitor import (
    DEGRADATION_THRESHOLD_PCT,
    ROLLBACK_EVENT_WINDOW,
    RollbackMonitor,
)
from afair.agents.tuner import (
    MAX_ROLLBACKS_PER_WEEK,
    ROLLBACK_COOLDOWN_DAYS,
    TIME_TRIGGER_SECONDS,
    Tuner,
)
from afair.substrate import open_db, tuner_state


@pytest.fixture()
def conn(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    c = open_db(vault)
    yield c
    c.close()


# ─── helpers ──────────────────────────────────────────────────────────────


def _seed_old_observation(conn) -> None:
    """Anchor the tuner's _last_cycle_at to the distant past so the
    time-trigger fires this cycle."""
    old_ts = (datetime.now(UTC) - timedelta(seconds=TIME_TRIGGER_SECONDS + 60)).isoformat()
    with conn:
        conn.execute(
            """
            INSERT INTO tuner_state (
                id, recorded_at, kind, worker, tunable,
                old_value_json, new_value_json, evidence_json, rationale
            ) VALUES (?, ?, 'observation', 'seed', 'seed', NULL, NULL, NULL, NULL)
            """,
            (f"seed_{time.time_ns()}", old_ts),
        )


def _seed_event(conn) -> None:
    """Insert one tiny remember event so salience replay has data."""
    from afair.substrate.events import write_event_with_status
    write_event_with_status(
        conn,
        kind="remember",
        origin="agent",
        payload={"content_type": "text", "text": "Sajinth runs Athara"},
    )


def _make_judge_report(*, b_share: float, pair_count: int = 30) -> JudgeReport:
    """Fabricate a JudgeReport with the requested variant share."""
    b_wins = round(pair_count * b_share)
    a_wins = pair_count - b_wins
    return JudgeReport(
        pair_count=pair_count,
        panel=("test/judge-a", "test/judge-b", "test/judge-c"),
        pair_verdicts=tuple(
            PanelVerdict(pair_index=i, winner="B" if i < b_wins else "A",
                         votes={"A": 0 if i < b_wins else 3, "B": 3 if i < b_wins else 0, "TIE": 0},
                         reasons=["test"])
            for i in range(pair_count)
        ),
        a_wins=a_wins,
        b_wins=b_wins,
        ties=0,
        a_share=a_wins / pair_count,
        b_share=b_wins / pair_count,
        tokens_spent_estimate=15_000,
        aborted=False,
        abort_reason=None,
    )


# ─── promote path ─────────────────────────────────────────────────────────


def test_phase_b_promotes_when_judge_majority_passes(conn) -> None:
    """With judge favoring variant ≥ 70%, the tuner writes a promote row."""
    from afair.settings import Settings

    _seed_old_observation(conn)
    _seed_event(conn)

    fake_report = _make_judge_report(b_share=0.80)
    with patch("afair.agents.tuner.judge_pairs", return_value=fake_report):
        stats = Tuner(promote_enabled=True).run(conn, Settings())

    promotes = [r for r in tuner_state.history(conn, limit=50) if r.kind == "promote"]
    assert stats["promoted"] is True
    assert len(promotes) == 1
    p = promotes[0]
    # Promote evidence includes baseline + judge stats.
    assert p.evidence is not None
    assert "pre_promote_baseline" in p.evidence
    assert p.evidence["judge_panel"]["b_share"] == 0.8


def test_phase_b_does_not_promote_when_judge_below_threshold(conn) -> None:
    """b_share below 0.70 → no promote, only observation."""
    from afair.settings import Settings

    _seed_old_observation(conn)
    _seed_event(conn)

    fake_report = _make_judge_report(b_share=0.50)
    with patch("afair.agents.tuner.judge_pairs", return_value=fake_report):
        stats = Tuner(promote_enabled=True).run(conn, Settings())

    promotes = [r for r in tuner_state.history(conn, limit=50) if r.kind == "promote"]
    assert stats["promoted"] is False
    assert promotes == []


def test_phase_b_judge_abort_blocks_promote(conn) -> None:
    """When the judge panel aborts (budget exhausted), no promote happens."""
    from afair.settings import Settings

    _seed_old_observation(conn)
    _seed_event(conn)

    aborted = JudgeReport(
        pair_count=5,
        panel=("test/judge",),
        pair_verdicts=(),
        a_wins=0, b_wins=0, ties=0,
        a_share=0.0, b_share=0.0,
        tokens_spent_estimate=200_000,
        aborted=True,
        abort_reason="token budget exhausted",
    )
    with patch("afair.agents.tuner.judge_pairs", return_value=aborted):
        Tuner(promote_enabled=True).run(conn, Settings())

    promotes = [r for r in tuner_state.history(conn, limit=50) if r.kind == "promote"]
    assert promotes == []


# ─── cooldown ────────────────────────────────────────────────────────────


def test_cooldown_skips_recently_rolled_back_tunable(conn) -> None:
    """If a tunable had a rollback within the cooldown window, the
    tuner skips it and picks a different one (or returns no hypothesis)."""
    from afair.settings import Settings

    # Plant a recent rollback on salience so the tuner has to pick
    # another worker. Use a real weights dict so the registry doesn't
    # see corrupted state when the cooldown check happens.
    valid_weights = json.dumps({
        "entity_density": 0.25, "link_density": 0.20, "has_conflict": 0.10,
        "type_hint_bump": 0.15, "is_compound": 0.10, "recency": 0.20,
    })
    recent = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    with conn:
        conn.execute(
            """
            INSERT INTO tuner_state (
                id, recorded_at, kind, worker, tunable,
                old_value_json, new_value_json, evidence_json, rationale
            ) VALUES ('rb1', ?, 'rollback', 'salience', 'component_weights',
                      ?, ?, NULL, 'test rollback')
            """,
            (recent, valid_weights, valid_weights),
        )

    _seed_old_observation(conn)
    fake_report = _make_judge_report(b_share=0.80)
    with patch("afair.agents.tuner.judge_pairs", return_value=fake_report):
        stats = Tuner(promote_enabled=True).run(conn, Settings())

    if stats["hypothesis"]:
        assert stats["hypothesis"]["worker"] != "salience"


def test_cooldown_expires_after_window(conn) -> None:
    """A rollback older than ROLLBACK_COOLDOWN_DAYS no longer blocks."""
    from afair.settings import Settings

    valid_weights = json.dumps({
        "entity_density": 0.25, "link_density": 0.20, "has_conflict": 0.10,
        "type_hint_bump": 0.15, "is_compound": 0.10, "recency": 0.20,
    })
    old_rb = (datetime.now(UTC) - timedelta(days=ROLLBACK_COOLDOWN_DAYS + 1)).isoformat()
    with conn:
        conn.execute(
            """
            INSERT INTO tuner_state (
                id, recorded_at, kind, worker, tunable,
                old_value_json, new_value_json, evidence_json, rationale
            ) VALUES ('rb_old', ?, 'rollback', 'salience', 'component_weights',
                      ?, ?, NULL, 'ancient rollback')
            """,
            (old_rb, valid_weights, valid_weights),
        )

    _seed_old_observation(conn)
    fake_report = _make_judge_report(b_share=0.80)
    with patch("afair.agents.tuner.judge_pairs", return_value=fake_report):
        stats = Tuner(promote_enabled=True).run(conn, Settings())

    # Old rollback no longer blocks — salience CAN be chosen again.
    # We don't assert salience specifically (diversity rotates), just
    # that the tuner produced a hypothesis at all.
    assert stats["hypothesis"] is not None


# ─── halt ─────────────────────────────────────────────────────────────────


def test_global_halt_after_too_many_rollbacks(conn) -> None:
    """More than MAX_ROLLBACKS_PER_WEEK rollbacks in 7 days → halted."""
    from afair.settings import Settings

    # Rollback rows from 5 days ago — still inside the 7-day halt
    # window, but old enough that the tuner's time trigger
    # (≥ 24h since last cycle) still fires.
    five_days_ago = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    with conn:
        for i in range(MAX_ROLLBACKS_PER_WEEK + 1):
            conn.execute(
                """
                INSERT INTO tuner_state (
                    id, recorded_at, kind, worker, tunable,
                    old_value_json, new_value_json, evidence_json, rationale
                ) VALUES (?, ?, 'rollback', ?, ?, '{}', '{}', NULL, 'test')
                """,
                (f"rb_{i}", five_days_ago, f"worker_{i}", f"tunable_{i}"),
            )

    stats = Tuner(promote_enabled=True).run(conn, Settings())

    assert stats["halted"] is True
    assert "rollbacks in last 7 days" in stats["halt_reason"]
    # No new promote rows.
    promotes = [r for r in tuner_state.history(conn, limit=100) if r.kind == "promote"]
    assert promotes == []


# ─── rollback monitor ─────────────────────────────────────────────────────


def _insert_promote(conn, *, worker, tunable, old_value, new_value,
                    recorded_at, baseline=None):
    """Direct INSERT helper for setting up promote rows in test fixtures."""
    evidence = {"pre_promote_baseline": baseline or {
        "useful_count": 0, "not_useful_count": 0, "sample_rows": 0,
    }}
    with conn:
        conn.execute(
            """
            INSERT INTO tuner_state (
                id, recorded_at, kind, worker, tunable,
                old_value_json, new_value_json, evidence_json, rationale
            ) VALUES (?, ?, 'promote', ?, ?, ?, ?, ?, ?)
            """,
            (
                f"p_{time.time_ns()}",
                recorded_at,
                worker,
                tunable,
                json.dumps(old_value),
                json.dumps(new_value),
                json.dumps(evidence),
                "test promote",
            ),
        )


def _insert_feedback_observation(conn, *, useful_ids, not_useful_ids, recorded_at):
    """Insert a recall.feedback observation row for the monitor to read."""
    with conn:
        conn.execute(
            """
            INSERT INTO tuner_state (
                id, recorded_at, kind, worker, tunable,
                old_value_json, new_value_json, evidence_json, rationale
            ) VALUES (?, ?, 'observation', 'recall', 'feedback',
                      NULL, NULL, ?, 'test feedback')
            """,
            (
                f"fb_{time.time_ns()}",
                recorded_at,
                json.dumps({
                    "useful_event_ids": useful_ids,
                    "not_useful_event_ids": not_useful_ids,
                    "missing_topic": None,
                }),
            ),
        )


def _insert_n_events(conn, n: int) -> None:
    """Bulk-insert `n` UNIQUE events so the monitor's event-count
    threshold passes. The payload must vary per row — identical
    payloads dedupe via content_hash (ON CONFLICT DO NOTHING)."""
    from afair.substrate.events import write_event_with_status
    for i in range(n):
        write_event_with_status(
            conn, kind="observe", origin="agent",
            payload={"content_type": "event", "action": f"x{i}", "subject": f"y{i}"},
        )


def test_monitor_skips_promote_under_event_window(conn) -> None:
    """Without enough new events since the promote, the monitor abstains."""
    from afair.settings import Settings

    recent = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    _insert_promote(
        conn, worker="surprise", tunable="context_window",
        old_value=20, new_value=25, recorded_at=recent,
    )
    # Only insert a handful of events — well below ROLLBACK_EVENT_WINDOW.
    _insert_n_events(conn, n=ROLLBACK_EVENT_WINDOW - 10)

    stats = RollbackMonitor().run(conn, Settings())
    assert stats["rollbacks_fired"] == 0
    rollbacks = [r for r in tuner_state.history(conn, limit=50) if r.kind == "rollback"]
    assert rollbacks == []


def test_monitor_fires_rollback_on_degradation(conn) -> None:
    """Useful-rate drop ≥ threshold → rollback fired, value restored."""
    from afair.settings import Settings

    recent = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    promote_ts = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

    # Pre-promote baseline: 8 useful, 2 not_useful (high rate).
    baseline = {"useful_count": 8, "not_useful_count": 2, "sample_rows": 5}
    _insert_promote(
        conn, worker="surprise", tunable="context_window",
        old_value=20, new_value=25, recorded_at=promote_ts, baseline=baseline,
    )

    # Post-promote signal: 1 useful, 9 not_useful (much worse rate).
    post_ts = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    _insert_feedback_observation(
        conn,
        useful_ids=["u1"],
        not_useful_ids=[f"n{i}" for i in range(9)],
        recorded_at=post_ts,
    )

    # ≥ ROLLBACK_EVENT_WINDOW new events since the promote.
    _insert_n_events(conn, n=ROLLBACK_EVENT_WINDOW + 5)

    _ = recent  # silence "unused" if branch fires
    stats = RollbackMonitor().run(conn, Settings())
    assert stats["rollbacks_fired"] == 1

    rollbacks = [r for r in tuner_state.history(conn, limit=50) if r.kind == "rollback"]
    assert len(rollbacks) == 1
    assert rollbacks[0].new_value == 20  # restored from old_value
    # Evidence captures the decision details.
    assert rollbacks[0].evidence is not None
    assert "decision" in rollbacks[0].evidence


def test_monitor_idempotent_on_already_rolled_back_promote(conn) -> None:
    """Once a promote has been rolled back, the monitor doesn't fire
    again on the same promote (a newer rollback supersedes it)."""
    from afair.settings import Settings

    promote_ts = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    _insert_promote(
        conn, worker="surprise", tunable="context_window",
        old_value=20, new_value=25, recorded_at=promote_ts,
    )
    # A rollback later supersedes this promote.
    rollback_ts = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    with conn:
        conn.execute(
            """
            INSERT INTO tuner_state (
                id, recorded_at, kind, worker, tunable,
                old_value_json, new_value_json, evidence_json, rationale
            ) VALUES ('rb_x', ?, 'rollback', 'surprise', 'context_window',
                      '25', '20', NULL, 'prior rollback')
            """,
            (rollback_ts,),
        )

    _insert_n_events(conn, n=ROLLBACK_EVENT_WINDOW + 5)
    stats = RollbackMonitor().run(conn, Settings())
    # The promote is no longer "active" — superseded by the rollback.
    assert stats["promotes_checked"] == 0


# ─── degradation threshold sanity ────────────────────────────────────────


def test_degradation_threshold_constant_matches_plan() -> None:
    """Plan §5 says 10% drop fires rollback. Constant must agree."""
    assert DEGRADATION_THRESHOLD_PCT == 10.0
