"""Tests for the tunable registry and its substrate-backed persistence."""

from __future__ import annotations

import pytest

from afair.agents.tunable_registry import (
    REGISTRY,
    ChangeRejected,
    TunableRegistry,
    TunableSpec,
    record_change,
    validate_change,
)
from afair.substrate import open_db, tuner_state


@pytest.fixture()
def conn(tmp_path):
    """Fresh substrate connection per test."""
    vault = tmp_path / "vault"
    vault.mkdir()
    c = open_db(vault)
    yield c
    c.close()


def _salience_spec() -> TunableSpec:
    return next(s for s in REGISTRY if s.worker == "salience" and s.tunable == "component_weights")


def _surprise_spec() -> TunableSpec:
    return next(s for s in REGISTRY if s.worker == "surprise" and s.tunable == "context_window")


# ─── default-value resolution ─────────────────────────────────────────────


def test_get_returns_static_default_when_no_promote_exists(conn) -> None:
    r = TunableRegistry(conn)
    weights = r.get("salience", "component_weights")
    assert weights == _salience_spec().default
    # All six components present and sum to 1.0.
    assert set(weights.keys()) == {
        "entity_density",
        "link_density",
        "has_conflict",
        "type_hint_bump",
        "is_compound",
        "recency",
    }
    assert abs(sum(weights.values()) - 1.0) < 0.01


def test_get_unknown_tunable_raises_keyerror(conn) -> None:
    r = TunableRegistry(conn)
    with pytest.raises(KeyError):
        r.get("not_a_worker", "not_a_tunable")


def test_int_default_returned(conn) -> None:
    r = TunableRegistry(conn)
    assert r.get("surprise", "context_window") == 20


# ─── promote / rollback flow ──────────────────────────────────────────────


def test_promote_then_get_returns_new_value(conn) -> None:
    r = TunableRegistry(conn)
    record_change(
        r,
        kind="promote",
        worker="surprise",
        tunable="context_window",
        old_value=20,
        new_value=25,
        evidence={"replay_size": 50, "judge_majority": 0.74},
        rationale="judge majority preferred 25 over 20",
    )
    assert r.get("surprise", "context_window") == 25


def test_rollback_restores_prior_value(conn) -> None:
    r = TunableRegistry(conn)
    record_change(
        r,
        kind="promote",
        worker="surprise",
        tunable="context_window",
        old_value=20,
        new_value=25,
        rationale="t=0",
    )
    record_change(
        r,
        kind="rollback",
        worker="surprise",
        tunable="context_window",
        old_value=25,
        new_value=20,
        rationale="degradation gate fired",
    )
    assert r.get("surprise", "context_window") == 20


def test_cache_invalidated_after_write(conn) -> None:
    r = TunableRegistry(conn)
    # First read populates the cache with the default.
    assert r.get("surprise", "context_window") == 20
    # Write through record_change → cache is invalidated → next get
    # reads fresh from substrate. +25% from 20 = 25, within the 30%
    # bounded_delta on surprise.context_window.
    record_change(
        r,
        kind="promote",
        worker="surprise",
        tunable="context_window",
        old_value=20,
        new_value=25,
        rationale="bigger window",
    )
    assert r.get("surprise", "context_window") == 25


def test_independent_registry_instances_see_writes(conn) -> None:
    r1 = TunableRegistry(conn)
    record_change(
        r1,
        kind="promote",
        worker="surprise",
        tunable="context_window",
        old_value=20,
        new_value=15,
        rationale="smaller",
    )
    # Fresh registry on same connection reads the substrate state.
    r2 = TunableRegistry(conn)
    assert r2.get("surprise", "context_window") == 15


# ─── change validation ───────────────────────────────────────────────────


def test_validate_float_within_delta_ok() -> None:
    spec = next(s for s in REGISTRY if s.worker == "mode_switcher" and s.tunable == "cen_threshold")
    # 8.0 -> 8.5 is +6.25% → well within 20% bounded_delta
    validate_change(spec=spec, current=8.0, proposed=8.5)


def test_validate_float_exceeds_delta_rejected() -> None:
    spec = next(s for s in REGISTRY if s.worker == "mode_switcher" and s.tunable == "cen_threshold")
    # 8.0 -> 11.0 is +37.5% → exceeds 20%
    with pytest.raises(ChangeRejected, match="delta"):
        validate_change(spec=spec, current=8.0, proposed=11.0)


def test_validate_float_above_max_rejected() -> None:
    spec = next(s for s in REGISTRY if s.worker == "mode_switcher" and s.tunable == "cen_threshold")
    with pytest.raises(ChangeRejected, match="above max"):
        validate_change(spec=spec, current=8.0, proposed=20.0)


def test_validate_float_below_min_rejected() -> None:
    spec = next(s for s in REGISTRY if s.worker == "mode_switcher" and s.tunable == "cen_threshold")
    with pytest.raises(ChangeRejected, match="below min"):
        validate_change(spec=spec, current=8.0, proposed=1.0)


def test_validate_int_ok() -> None:
    spec = _surprise_spec()
    validate_change(spec=spec, current=20, proposed=25)  # +25% within 30% delta


def test_validate_int_wrong_type_rejected() -> None:
    spec = _surprise_spec()
    with pytest.raises(ChangeRejected, match="expected int"):
        validate_change(spec=spec, current=20, proposed=25.0)


def test_validate_weights_dict_sum_ok() -> None:
    spec = _salience_spec()
    # Tiny shift, each within ±20%, sum = 1.0.
    proposed = {
        "entity_density": 0.27,
        "link_density": 0.18,
        "has_conflict": 0.10,
        "type_hint_bump": 0.15,
        "is_compound": 0.10,
        "recency": 0.20,
    }
    validate_change(spec=spec, current=spec.default, proposed=proposed)


def test_validate_weights_dict_sum_not_one_rejected() -> None:
    spec = _salience_spec()
    proposed = {**spec.default, "entity_density": spec.default["entity_density"] + 0.05}
    with pytest.raises(ChangeRejected, match="sum"):
        validate_change(spec=spec, current=spec.default, proposed=proposed)


def test_validate_weights_dict_missing_key_rejected() -> None:
    spec = _salience_spec()
    proposed = {k: v for k, v in spec.default.items() if k != "recency"}
    proposed["entity_density"] = 0.45  # try to keep sum=1.0 to isolate the missing-key check
    with pytest.raises(ChangeRejected, match="wrong keys"):
        validate_change(spec=spec, current=spec.default, proposed=proposed)


# ─── tuner_state direct ──────────────────────────────────────────────────


def test_tuner_state_history_filters(conn) -> None:
    tuner_state.write(
        conn,
        kind="hypothesis",
        worker="surprise",
        tunable="context_window",
        new_value=25,
        rationale="judge says try larger",
    )
    tuner_state.write(
        conn,
        kind="promote",
        worker="surprise",
        tunable="context_window",
        old_value=20,
        new_value=25,
    )
    tuner_state.write(
        conn,
        kind="observation",
        worker="salience",
        tunable="component_weights",
        evidence={"judge_split": 0.6},
    )

    surprise_history = tuner_state.history(conn, worker="surprise")
    assert len(surprise_history) == 2
    assert all(e.worker == "surprise" for e in surprise_history)

    salience_history = tuner_state.history(conn, worker="salience")
    assert len(salience_history) == 1
    assert salience_history[0].kind == "observation"


def test_tuner_state_invalid_kind_blocked_at_db_level(conn) -> None:
    # CHECK constraint should reject anything outside the allow-list.
    import sqlite3 as _sqlite3

    with pytest.raises(_sqlite3.IntegrityError):
        tuner_state.write(
            conn,
            kind="totally-invalid",  # type: ignore[arg-type]
            worker="x",
            tunable="y",
        )


def test_tuner_state_no_update_no_delete(conn) -> None:
    import sqlite3 as _sqlite3

    e = tuner_state.write(conn, kind="hypothesis", worker="x", tunable="y", new_value=1)
    with pytest.raises(_sqlite3.IntegrityError, match="append-only"), conn:
        conn.execute("UPDATE tuner_state SET rationale = 'changed' WHERE id = ?", (e.id,))
    with pytest.raises(_sqlite3.IntegrityError, match="append-only"), conn:
        conn.execute("DELETE FROM tuner_state WHERE id = ?", (e.id,))


# ── ADR-0004 S8: edge-confidence + belief tunables ──────────────────────────


def _spec(worker: str, tunable: str) -> TunableSpec:
    return next(s for s in REGISTRY if s.worker == worker and s.tunable == tunable)


def test_edge_confidence_tunables_registered_with_module_defaults() -> None:
    from afair.substrate.belief import _MIN_AUTO_CONFIRM_CONFIDENCE
    from afair.substrate.confidence import DEFAULT_BASE_RATE, W_CORROBORATION

    assert _spec("edge_confidence", "base_rate").default == DEFAULT_BASE_RATE
    assert _spec("edge_confidence", "corroboration_weight").default == W_CORROBORATION
    assert _spec("belief", "auto_confirm_floor").default == _MIN_AUTO_CONFIRM_CONFIDENCE


def test_edge_confidence_bounds_reject_out_of_range() -> None:
    base = _spec("edge_confidence", "base_rate")
    # Above max (0.85) is rejected.
    with pytest.raises(ChangeRejected):
        validate_change(spec=base, current=0.70, proposed=0.95)
    # A small in-bounds move passes.
    validate_change(spec=base, current=0.70, proposed=0.73)

    floor = _spec("belief", "auto_confirm_floor")
    with pytest.raises(ChangeRejected):
        validate_change(spec=floor, current=0.75, proposed=0.50)  # below min 0.60
    validate_change(spec=floor, current=0.75, proposed=0.78)


def test_registry_resolves_promoted_edge_confidence_value(conn) -> None:
    r = TunableRegistry(conn)
    assert r.get("edge_confidence", "base_rate") == 0.70  # default
    tuner_state.write(
        conn,
        kind="promote",
        worker="edge_confidence",
        tunable="base_rate",
        old_value=0.70,
        new_value=0.75,
        rationale="test",
    )
    r.invalidate("edge_confidence", "base_rate")
    assert r.get("edge_confidence", "base_rate") == 0.75
