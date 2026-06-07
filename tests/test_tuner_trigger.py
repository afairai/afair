"""Regression tests for the tuner cycle trigger (_should_run / _last_cycle_at).

Guards against the cold-start deadlock that left the self-improvement loop
inert in production: on a fresh vault there was no prior tuner_state row, so
_last_cycle_at returned `now`; _should_run then saw ~0s elapsed + 0 new
events and returned False. The tuner therefore never wrote the first row
that would have let it consider itself due, and never cycled again.

A second guard: an external recall.feedback observation row (written by the
recall handler, worker="recall") must NOT be mistaken for a tuner cycle.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from afair.agents.tuner import Tuner
from afair.substrate import open_db


@pytest.fixture()
def conn(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    c = open_db(vault)
    yield c
    c.close()


def _insert_tuner_row(conn, *, kind: str, worker: str, tunable: str, recorded_at: str) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO tuner_state (
                id, recorded_at, kind, worker, tunable,
                old_value_json, new_value_json, evidence_json, rationale
            ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)
            """,
            (f"row_{time.time_ns()}", recorded_at, kind, worker, tunable),
        )


def test_should_run_fires_on_cold_start(conn) -> None:
    """Empty tuner_state → the very first cycle must fire (no deadlock)."""
    tuner = Tuner(promote_enabled=False)
    assert tuner._last_cycle_at(conn) is None
    assert tuner._should_run(conn) is True


def test_recall_feedback_row_is_not_a_tuner_cycle(conn) -> None:
    """A recall.feedback row is an external reward signal, not a tuner
    cycle: it must not anchor _last_cycle_at, so the tuner still bootstraps."""
    _insert_tuner_row(
        conn,
        kind="observation",
        worker="recall",
        tunable="feedback",
        recorded_at="2026-06-07T10:52:50+00:00",
    )
    tuner = Tuner(promote_enabled=False)
    assert tuner._last_cycle_at(conn) is None
    assert tuner._should_run(conn) is True


def test_recent_tuner_cycle_suppresses_rerun(conn) -> None:
    """A genuine recent tuner observation anchors _last_cycle_at, so
    _should_run stays False until the time/traffic trigger (no events
    seeded here). Guards against over-correcting into always-run."""
    now_iso = datetime.now(UTC).isoformat()
    _insert_tuner_row(conn, kind="observation", worker="tuner", tunable="meta", recorded_at=now_iso)
    tuner = Tuner(promote_enabled=False)
    assert tuner._last_cycle_at(conn) is not None
    assert tuner._should_run(conn) is False
