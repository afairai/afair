"""Tests for the invariant guards and the tuner skeleton.

Tuner is observe-only in Phase A; tests verify:
  * Guards reject obvious failures.
  * Tuner writes a hypothesis + observation per cycle.
  * Tuner does NOT promote (promote_enabled=False default).
  * Tuner respects the traffic + time triggers.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from afair.agents.guards import (
    check_canonicalizer_merges,
    check_consolidator_outputs,
    check_extractor_outputs,
    check_mode_switcher_outputs,
    check_mode_switcher_thresholds,
    check_salience_outputs,
)
from afair.agents.tuner import (
    TIME_TRIGGER_SECONDS,
    Tuner,
)
from afair.substrate import open_db, tuner_state

# ─── guards: salience ─────────────────────────────────────────────────────


def test_salience_guard_passes_valid_outputs() -> None:
    good = [
        {
            "salience": 0.5,
            "salience_components": {
                "entity_density": 0.2,
                "link_density": 0.1,
                "has_conflict": 0.0,
                "type_hint_bump": 0.1,
                "is_compound": 0.0,
                "recency": 0.1,
            },
        }
    ]
    assert check_salience_outputs(good).passed


def test_salience_guard_rejects_score_out_of_range() -> None:
    bad = [
        {
            "salience": 1.5,
            "salience_components": {
                "entity_density": 0,
                "link_density": 0,
                "has_conflict": 0,
                "type_hint_bump": 0,
                "is_compound": 0,
                "recency": 0,
            },
        }
    ]
    r = check_salience_outputs(bad)
    assert not r.passed
    assert any("outside [0, 1]" in f for f in r.failures)


def test_salience_guard_rejects_wrong_keys() -> None:
    bad = [{"salience": 0.5, "salience_components": {"entity_density": 0.5}}]
    r = check_salience_outputs(bad)
    assert not r.passed
    assert any("components keys wrong" in f for f in r.failures)


def test_salience_guard_empty_input_is_pass() -> None:
    assert check_salience_outputs([]).passed


# ─── guards: mode_switcher ────────────────────────────────────────────────


def test_mode_switcher_guard_valid() -> None:
    assert check_mode_switcher_outputs(["cen", "dmn", "cen"]).passed


def test_mode_switcher_guard_rejects_garbage() -> None:
    r = check_mode_switcher_outputs(["cen", "WAT"])
    assert not r.passed


def test_mode_switcher_threshold_hysteresis_holds() -> None:
    assert check_mode_switcher_thresholds(cen_threshold=8.0, dmn_threshold=4.0).passed


def test_mode_switcher_threshold_inverted_rejected() -> None:
    r = check_mode_switcher_thresholds(cen_threshold=3.0, dmn_threshold=5.0)
    assert not r.passed
    assert "hysteresis violated" in r.failures[0]


# ─── guards: extractor ────────────────────────────────────────────────────


def test_extractor_guard_valid() -> None:
    good = [{"summary": "A real summary.", "salient_facts": ["fact one", "fact two"]}]
    assert check_extractor_outputs(good).passed


def test_extractor_guard_rejects_empty_summary() -> None:
    bad = [{"summary": "  ", "salient_facts": ["x"]}]
    r = check_extractor_outputs(bad)
    assert not r.passed


def test_extractor_guard_rejects_empty_facts() -> None:
    bad = [{"summary": "s", "salient_facts": []}]
    r = check_extractor_outputs(bad)
    assert not r.passed


# ─── guards: consolidator + canonicalizer ────────────────────────────────


def test_consolidator_guard_rejects_missing_parents() -> None:
    bad = [{"summary": "ok", "parent_hashes": []}]
    r = check_consolidator_outputs(bad)
    assert not r.passed


def test_canonicalizer_guard_valid_merges() -> None:
    good = [{"source_entity_name": "Sajinth", "target_entity_name": "Saji"}]
    assert check_canonicalizer_merges(good).passed


# ─── tuner: triggers + observe-only behavior ─────────────────────────────


@pytest.fixture()
def conn(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    c = open_db(vault)
    yield c
    c.close()


def test_tuner_promote_disabled_by_default() -> None:
    """Safe-by-default: a bare ``Tuner()`` must never promote.

    Production wires ``Tuner(promote_enabled=False)`` explicitly; the
    class default has to match so a direct ``Tuner()`` (a test, a
    self-hoster script, a future refactor) can't silently get live
    promotion. Opt-in stays possible via the explicit flag.
    """
    assert Tuner().promote_enabled is False
    assert Tuner(promote_enabled=True).promote_enabled is True
    assert Tuner(promote_enabled=False).promote_enabled is False


def test_tuner_first_boot_triggers(conn) -> None:
    """No prior cycle → the tuner must bootstrap and fire its first cycle.

    This previously asserted the opposite (``triggered is False``), which
    codified a cold-start deadlock: the tuner only recorded its first cycle
    marker by running, but only ran if a marker already existed, so it never
    ran at all in production. See tests/test_tuner_trigger.py.
    """
    assert Tuner()._should_run(conn) is True


def _seed_old_observation(conn) -> None:
    """Insert an observation row with a backdated recorded_at directly.

    We bypass tuner_state.write so the no-update trigger doesn't fire
    later, while still respecting the no-update / no-delete contract
    on subsequent ops.
    """
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


def test_tuner_runs_after_time_trigger(conn) -> None:
    """With an aged prior observation, the tuner cycle fires.

    Use promote_enabled=False to keep this test offline. The Phase-B
    judge path is exercised separately in test_tuner_phase_b.py.
    """
    from afair.settings import Settings

    _seed_old_observation(conn)
    t = Tuner(promote_enabled=False)
    stats = t.run(conn, Settings())
    assert stats["triggered"] is True
    assert stats["hypothesis"] is not None
    # Diversity rotates through the whitelist; with no prior tuner
    # rows, the first spec in REGISTRY (salience) wins.
    assert stats["hypothesis"]["worker"] in {
        "salience",
        "mode_switcher",
        "surprise",
        "entity_canonicalizer",
        "consolidator",
    }


def test_tuner_writes_observation_row(conn) -> None:
    """Each cycle emits a hypothesis row AND an observation row."""
    from afair.settings import Settings

    _seed_old_observation(conn)
    Tuner(promote_enabled=False).run(conn, Settings())

    rows = tuner_state.history(conn, limit=50)
    kinds = [r.kind for r in rows]
    assert "hypothesis" in kinds
    assert "observation" in kinds


def test_tuner_does_not_promote_when_disabled(conn) -> None:
    """promote_enabled=False — no promote rows under any path."""
    from afair.settings import Settings

    _seed_old_observation(conn)
    Tuner(promote_enabled=False).run(conn, Settings())

    rows = tuner_state.history(conn, limit=50)
    assert all(r.kind != "promote" for r in rows)
    assert all(r.kind != "rollback" for r in rows)


def test_tuner_observation_includes_phase_marker(conn) -> None:
    from afair.settings import Settings

    _seed_old_observation(conn)
    Tuner(promote_enabled=False).run(conn, Settings())

    obs_rows = [r for r in tuner_state.history(conn, limit=50) if r.kind == "observation"]
    # At least one observation written; promote_enabled=False is
    # reflected in either the judge_panel marker or the rationale.
    assert obs_rows
    found = any(
        (
            isinstance(r.evidence, dict)
            and r.evidence.get("judge_panel") == "skipped:promote_enabled_false"
        )
        or (r.rationale and "promote_enabled=False" in r.rationale)
        or (
            isinstance(r.evidence, dict)
            and r.evidence.get("judge_panel") == "skipped:no_replay_shape"
        )
        for r in obs_rows
    )
    assert found, f"expected a phase marker observation; got {[r.evidence for r in obs_rows]}"
