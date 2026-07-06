"""
Tunable registry — the single source of truth for everything the
self-improvement tuner is allowed to touch.

The registry has three jobs:

1. **Declare** the whitelist of tunables: which (worker, parameter)
   pairs exist, what type each is, what the default value is, what
   bounds it must stay within, and how much it may change per promote.
   Anything not declared here is OFF-LIMITS to the tuner. The MCP
   verbs, the substrate schema, and worker code structure are
   deliberately absent and stay frozen.

2. **Resolve** values at runtime: each worker calls
   ``registry.get(worker, tunable)`` to read the active value. The
   registry checks the ``tuner_state`` table for the latest promote /
   rollback; if none exists, it falls back to the declared default.

3. **Validate** proposed changes: the tuner submits a candidate via
   ``registry.validate_change(worker, tunable, new_value)`` before
   any promotion. The check enforces type, hard bounds, and the
   bounded-delta rule (max ±20% movement per promote for floats; one
   item swap for prompt-variant pools).

Reading values is the hot path — workers call ``get`` on every event
they score. The registry caches resolved values in-memory; the cache
is invalidated when the tuner writes a new row to ``tuner_state``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import structlog

from ..substrate import tuner_state

if TYPE_CHECKING:
    import sqlite3


log = structlog.get_logger(__name__)


TunableKind = Literal["float", "int", "string", "weights_dict"]


@dataclass(frozen=True)
class TunableSpec:
    """One row of the whitelist.

    Attributes:
        worker:        Owner worker name, e.g. ``"salience"``.
        tunable:       Parameter name within that worker.
        kind:          Type tag — ``float``, ``int``, ``string``, or
                       ``weights_dict`` (a dict mapping string keys to
                       float values that sum to 1.0).
        default:       Static default used when no tuner_state row exists.
                       Matches the historical hardcoded constants the
                       worker shipped with.
        min_value:     Hard lower bound (inclusive). Optional.
        max_value:     Hard upper bound (inclusive). Optional.
        bounded_delta: Max fractional change per promote (e.g. 0.20 = ±20%).
                       Only meaningful for float / int. Ignored for string
                       (those swap via the variant pool) and weights_dict
                       (those re-normalize together; check applies to each
                       component independently).
        rationale:     One-sentence comment describing what the parameter
                       controls. Surfaced in admin queries.
    """

    worker: str
    tunable: str
    kind: TunableKind
    default: Any
    min_value: Any | None = None
    max_value: Any | None = None
    bounded_delta: float = 0.20
    rationale: str = ""
    # For string tunables: the explicit set of allowed values.
    # For weights_dict: the set of allowed component keys.
    allowed_values: frozenset[str] = field(default_factory=frozenset)


# ─── the whitelist ────────────────────────────────────────────────────────
#
# This list is the explicit contract referenced by VISION.md §4 I7.
# Every entry here is allowed to drift; nothing outside is. To extend
# the surface (e.g. tune a new parameter), add a row here AND make the
# corresponding worker read from registry.get(...) at runtime.

REGISTRY: tuple[TunableSpec, ...] = (
    # ── salience-worker component weights ─────────────────────────────
    # All six weights live in one weights_dict so the registry can
    # re-normalize after each change (sum stays 1.0).
    TunableSpec(
        worker="salience",
        tunable="component_weights",
        kind="weights_dict",
        default={
            "entity_density": 0.25,
            "link_density": 0.20,
            "has_conflict": 0.10,
            "type_hint_bump": 0.15,
            "is_compound": 0.10,
            "recency": 0.20,
        },
        min_value=0.0,
        max_value=1.0,
        bounded_delta=0.20,
        allowed_values=frozenset(
            {
                "entity_density",
                "link_density",
                "has_conflict",
                "type_hint_bump",
                "is_compound",
                "recency",
            }
        ),
        rationale="Per-component weight for the salience score; sums to 1.0.",
    ),
    # ── mode-switcher hysteresis thresholds ───────────────────────────
    TunableSpec(
        worker="mode_switcher",
        tunable="cen_threshold",
        kind="float",
        default=8.0,
        min_value=5.0,
        max_value=12.0,
        bounded_delta=0.20,
        rationale="Cumulative-salience threshold above which mode flips to CEN.",
    ),
    TunableSpec(
        worker="mode_switcher",
        tunable="dmn_threshold",
        kind="float",
        default=4.0,
        min_value=2.0,
        max_value=6.0,
        bounded_delta=0.20,
        rationale="Cumulative-salience threshold below which mode flips to DMN.",
    ),
    # ── surprise scorer ───────────────────────────────────────────────
    TunableSpec(
        worker="surprise",
        tunable="context_window",
        kind="int",
        default=20,
        min_value=10,
        max_value=50,
        bounded_delta=0.30,  # int gets a bit more headroom since 20% of 20 is 4
        rationale="Number of recent events the surprise scorer compares against.",
    ),
    # ── entity canonicalizer ──────────────────────────────────────────
    TunableSpec(
        worker="entity_canonicalizer",
        tunable="llm_escalation_threshold",
        kind="float",
        default=0.75,
        min_value=0.50,
        max_value=0.95,
        bounded_delta=0.15,
        rationale=(
            "Confidence threshold below which entity matching escalates from Haiku to Sonnet."
        ),
    ),
    # ── consolidator ──────────────────────────────────────────────────
    TunableSpec(
        worker="consolidator",
        tunable="salience_cutoff",
        kind="float",
        default=0.50,
        min_value=0.30,
        max_value=0.80,
        bounded_delta=0.20,
        rationale="Only events at or above this salience are included in the daily roundup.",
    ),
    # ── schema evolver (ADR-0003 Phase 4) ─────────────────────────────
    # Only the two signal THRESHOLDS are tunable. The evolver's guardrails
    # (per-cycle caps, the 30-day cooldown, the slug format, sample
    # binding) are hard constants in agents/schema_evolver.py — per I7 the
    # worker's own fences stay off the self-modification surface.
    TunableSpec(
        worker="schema_evolver",
        tunable="other_share_threshold",
        kind="float",
        default=0.20,
        min_value=0.05,
        max_value=0.60,
        bounded_delta=0.20,
        rationale=(
            "Share of live entities in 'other' above which the evolver proposes "
            "carving a new kind out of it."
        ),
    ),
    TunableSpec(
        worker="schema_evolver",
        tunable="promote_min_entities",
        kind="int",
        default=10,
        min_value=3,
        max_value=100,
        bounded_delta=0.30,
        rationale=(
            "Distinct entities a normalized-away raw kind must recur on before "
            "the evolver proposes promoting it to a registry kind."
        ),
    ),
    # ── edge-confidence model (ADR-0004) ──────────────────────────────
    # The intercept + corroboration weight of the log-odds edge-confidence
    # model, and the served-confidence floor the quarantine gate uses. The
    # model's STRUCTURE (the terms, the sigmoid, the scorer itself) stays off
    # the tunable surface per I7; only these three scalars drift, within hard
    # bounds, on the evidence of calibration_report.
    TunableSpec(
        worker="edge_confidence",
        tunable="base_rate",
        kind="float",
        default=0.70,
        min_value=0.55,
        max_value=0.85,
        bounded_delta=0.10,
        rationale=(
            "Prior probability an evidence-gated agent-derived edge is true; "
            "the calibration intercept."
        ),
    ),
    TunableSpec(
        worker="edge_confidence",
        tunable="corroboration_weight",
        kind="float",
        default=0.8,
        min_value=0.2,
        max_value=2.0,
        bounded_delta=0.20,
        rationale=("Log-odds added per doubling of independent corroborating source events."),
    ),
    TunableSpec(
        worker="belief",
        tunable="auto_confirm_floor",
        kind="float",
        default=0.75,
        min_value=0.60,
        max_value=0.90,
        bounded_delta=0.10,
        rationale=(
            "Served-confidence floor below which a derived edge is quarantined as proposed."
        ),
    ),
    TunableSpec(
        worker="entity_dedup",
        tunable="kind_unify_floor",
        kind="float",
        default=0.85,
        min_value=0.75,
        max_value=0.95,
        bounded_delta=0.05,
        rationale=(
            "Same-entity confidence at/above which a cross-kind dedup merge "
            "auto-applies the LLM's unified kind instead of queueing a "
            "merge_review. Evidence: merge_review confirm/reject outcomes."
        ),
    ),
    # NOTE: extractor prompt-variant pools (per kind: text / pdf / audio
    # / vision) are intentionally NOT in the initial whitelist. They
    # need a separate "prompt variant pool" plumbing (Phase C) before
    # the tuner is allowed to swap them. Each is also a long string
    # change with higher blast radius — defer until Phase A is proven.
)


def _spec_lookup() -> dict[tuple[str, str], TunableSpec]:
    return {(s.worker, s.tunable): s for s in REGISTRY}


_SPECS = _spec_lookup()


# ─── runtime resolver ─────────────────────────────────────────────────────
#
# Workers call `registry.get(worker, tunable)` on every event they score.
# The substrate read is cheap (single indexed SELECT) but we cache anyway
# so a high-traffic worker isn't paying it per call. Invalidation: when
# the tuner writes a tuner_state row via :func:`record_change`, we drop
# the in-memory cache for that key.


class TunableRegistry:
    """Connection-scoped reader. Construct one per substrate connection.

    Thread-safe for concurrent reads — uses a single lock around the
    in-memory cache. Writes (cache invalidation) go through the same
    lock so reader/writer race against ``tuner_state`` is impossible.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._cache: dict[tuple[str, str], Any] = {}
        self._lock = threading.Lock()

    def get(self, worker: str, tunable: str) -> Any:
        """Return the active value, or raise KeyError if the (worker, tunable) is not whitelisted."""
        key = (worker, tunable)
        spec = _SPECS.get(key)
        if spec is None:
            raise KeyError(f"tunable not on whitelist: {worker}.{tunable}")
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            stored = tuner_state.current_value(self._conn, worker=worker, tunable=tunable)
            value = stored if stored is not None else spec.default
            self._cache[key] = value
            return value

    def invalidate(self, worker: str, tunable: str) -> None:
        """Drop the cached value for one key. Called by the tuner after writing."""
        with self._lock:
            self._cache.pop((worker, tunable), None)

    def invalidate_all(self) -> None:
        """Drop the entire cache. Used by tests and on full registry reload."""
        with self._lock:
            self._cache.clear()

    @property
    def connection(self) -> sqlite3.Connection:
        """Public access to the underlying substrate connection.

        Exposed so helpers like :func:`record_change` don't need to
        reach into a private attribute. The connection is borrowed,
        not owned — callers MUST NOT close it.
        """
        return self._conn

    def get_spec(self, worker: str, tunable: str) -> TunableSpec:
        """Return the spec row for (worker, tunable). Raises KeyError if not whitelisted."""
        spec = _SPECS.get((worker, tunable))
        if spec is None:
            raise KeyError(f"tunable not on whitelist: {worker}.{tunable}")
        return spec

    def list_tunables(self) -> tuple[TunableSpec, ...]:
        """Return the full whitelist. Read-only — modifying the returned tuple does nothing."""
        return REGISTRY


# ─── change validation (called by the tuner before any promote) ───────────


class ChangeRejected(Exception):
    """Raised when a proposed change violates the registry's contract."""


def validate_change(
    *,
    spec: TunableSpec,
    current: Any,
    proposed: Any,
) -> None:
    """Raise ChangeRejected if the proposed value violates spec.

    Checks:
        - Type matches the declared kind.
        - Hard bounds (min_value / max_value) respected.
        - Bounded-delta rule: |proposed - current| / |current| ≤ bounded_delta
          (for floats/ints). For weights_dict, each component must respect
          bounded_delta independently AND the dict must sum to 1.0 within
          a small tolerance.
        - For string: proposed value must be in allowed_values.
    """
    if spec.kind == "float":
        _validate_float(spec, current, proposed)
    elif spec.kind == "int":
        _validate_int(spec, current, proposed)
    elif spec.kind == "string":
        _validate_string(spec, proposed)
    elif spec.kind == "weights_dict":
        _validate_weights_dict(spec, current, proposed)
    else:
        raise ChangeRejected(f"unknown tunable kind: {spec.kind}")


def _validate_float(spec: TunableSpec, current: Any, proposed: Any) -> None:
    if not isinstance(proposed, (int, float)):
        raise ChangeRejected(f"expected float, got {type(proposed).__name__}")
    p = float(proposed)
    if spec.min_value is not None and p < spec.min_value:
        raise ChangeRejected(f"proposed {p} below min {spec.min_value}")
    if spec.max_value is not None and p > spec.max_value:
        raise ChangeRejected(f"proposed {p} above max {spec.max_value}")
    c = float(current)
    if c != 0.0:
        delta = abs(p - c) / abs(c)
        if delta > spec.bounded_delta:
            raise ChangeRejected(
                f"delta {delta:.3f} exceeds bound {spec.bounded_delta:.3f} "
                f"(current={c}, proposed={p})",
            )


def _validate_int(spec: TunableSpec, current: Any, proposed: Any) -> None:
    if not isinstance(proposed, int) or isinstance(proposed, bool):
        raise ChangeRejected(f"expected int, got {type(proposed).__name__}")
    if spec.min_value is not None and proposed < spec.min_value:
        raise ChangeRejected(f"proposed {proposed} below min {spec.min_value}")
    if spec.max_value is not None and proposed > spec.max_value:
        raise ChangeRejected(f"proposed {proposed} above max {spec.max_value}")
    if current != 0:
        delta = abs(proposed - current) / abs(current)
        if delta > spec.bounded_delta:
            raise ChangeRejected(
                f"delta {delta:.3f} exceeds bound {spec.bounded_delta:.3f}",
            )


def _validate_string(spec: TunableSpec, proposed: Any) -> None:
    if not isinstance(proposed, str):
        raise ChangeRejected(f"expected str, got {type(proposed).__name__}")
    if spec.allowed_values and proposed not in spec.allowed_values:
        raise ChangeRejected(
            f"proposed value not in allowed_values (allowed={sorted(spec.allowed_values)})",
        )


def _validate_weights_dict(spec: TunableSpec, current: Any, proposed: Any) -> None:
    if not isinstance(proposed, dict):
        raise ChangeRejected(f"expected dict, got {type(proposed).__name__}")
    proposed_keys = frozenset(proposed.keys())
    if spec.allowed_values and proposed_keys != spec.allowed_values:
        missing = spec.allowed_values - proposed_keys
        extra = proposed_keys - spec.allowed_values
        msg = "weights_dict has wrong keys"
        if missing:
            msg += f" (missing={sorted(missing)})"
        if extra:
            msg += f" (extra={sorted(extra)})"
        raise ChangeRejected(msg)
    # Each component value: type + per-component bound check
    for k, v in proposed.items():
        if not isinstance(v, (int, float)):
            raise ChangeRejected(f"weight[{k}] is not numeric")
        fv = float(v)
        if spec.min_value is not None and fv < spec.min_value:
            raise ChangeRejected(f"weight[{k}] = {fv} below min {spec.min_value}")
        if spec.max_value is not None and fv > spec.max_value:
            raise ChangeRejected(f"weight[{k}] = {fv} above max {spec.max_value}")
        c = float(current.get(k, 0.0)) if isinstance(current, dict) else 0.0
        if c != 0.0:
            delta = abs(fv - c) / abs(c)
            if delta > spec.bounded_delta:
                raise ChangeRejected(
                    f"weight[{k}] delta {delta:.3f} exceeds bound {spec.bounded_delta:.3f}",
                )
    total = sum(float(v) for v in proposed.values())
    # Tolerance: SUMS must round to ≈ 1.0. Tolerance ±0.01 catches
    # floating-point drift; anything bigger means a real bug.
    if abs(total - 1.0) > 0.01:
        raise ChangeRejected(
            f"weights sum {total:.4f} not within 0.01 of 1.0",
        )


# ─── tuner-facing write helper ────────────────────────────────────────────


def record_change(
    registry: TunableRegistry,
    *,
    kind: Literal["promote", "rollback"],
    worker: str,
    tunable: str,
    old_value: Any,
    new_value: Any,
    evidence: dict[str, Any] | None = None,
    rationale: str | None = None,
) -> None:
    """Persist a promote or rollback, then invalidate the registry cache.

    Defense-in-depth: validates the proposed change against the
    spec one more time. The tuner is supposed to call
    :func:`validate_change` before reaching here, but if a future
    bug skips that step, this catches it. Rollbacks are also
    validated (must respect bounds + delta) so a future "rollback
    to an unsafe historical value" can't smuggle a bad value
    through.

    Also enforces cross-tunable invariants (e.g. CEN > DMN for the
    mode_switcher) that single-spec validation can't catch.

    Cache invalidation happens AFTER the substrate write so that a
    failed write (rare, but possible in adversarial test conditions)
    doesn't leave the cache wrongly cleared.
    """
    spec = registry.get_spec(worker, tunable)
    # Validate the proposed value. Skip the bounded-delta check for
    # rollbacks because rollbacks intentionally make larger moves
    # (returning to a known-good prior value). They still respect
    # min/max + type rules.
    if kind == "rollback":
        _validate_bounds_only(spec=spec, proposed=new_value)
    else:
        validate_change(spec=spec, current=old_value, proposed=new_value)

    # Cross-tunable invariants the per-spec validator can't see.
    _validate_cross_tunable(registry, worker=worker, tunable=tunable, new_value=new_value)

    tuner_state.write(
        registry.connection,
        kind=kind,
        worker=worker,
        tunable=tunable,
        old_value=old_value,
        new_value=new_value,
        evidence=evidence,
        rationale=rationale,
    )
    registry.invalidate(worker, tunable)


def _validate_bounds_only(*, spec: TunableSpec, proposed: Any) -> None:
    """Type + min/max check without bounded_delta. Used for rollbacks."""
    if spec.kind == "float":
        if not isinstance(proposed, (int, float)):
            raise ChangeRejected(f"expected float, got {type(proposed).__name__}")
        p = float(proposed)
        if spec.min_value is not None and p < spec.min_value:
            raise ChangeRejected(f"proposed {p} below min {spec.min_value}")
        if spec.max_value is not None and p > spec.max_value:
            raise ChangeRejected(f"proposed {p} above max {spec.max_value}")
    elif spec.kind == "int":
        if not isinstance(proposed, int) or isinstance(proposed, bool):
            raise ChangeRejected(f"expected int, got {type(proposed).__name__}")
        if spec.min_value is not None and proposed < spec.min_value:
            raise ChangeRejected(f"proposed {proposed} below min {spec.min_value}")
        if spec.max_value is not None and proposed > spec.max_value:
            raise ChangeRejected(f"proposed {proposed} above max {spec.max_value}")
    elif spec.kind == "string":
        _validate_string(spec, proposed)
    elif spec.kind == "weights_dict":
        # For dict rollback we still verify keys + sum-to-1.0; per-component
        # bounded_delta is the only thing we skip.
        if not isinstance(proposed, dict):
            raise ChangeRejected(f"expected dict, got {type(proposed).__name__}")
        if spec.allowed_values and frozenset(proposed.keys()) != spec.allowed_values:
            raise ChangeRejected("weights_dict has wrong keys")
        total = sum(float(v) for v in proposed.values())
        if abs(total - 1.0) > 0.01:
            raise ChangeRejected(f"weights sum {total:.4f} not within 0.01 of 1.0")


def _validate_cross_tunable(
    registry: TunableRegistry,
    *,
    worker: str,
    tunable: str,
    new_value: Any,
) -> None:
    """Invariants that span more than one tunable.

    Currently only the mode_switcher's hysteresis rule (CEN must
    stay strictly greater than DMN). Without this check, a tuner
    could push dmn_threshold up beyond cen_threshold and the mode
    would flap on every event.
    """
    if worker != "mode_switcher":
        return
    if tunable == "cen_threshold":
        dmn = float(registry.get("mode_switcher", "dmn_threshold"))
        if float(new_value) <= dmn:
            raise ChangeRejected(
                f"cen_threshold {new_value} must remain strictly greater than "
                f"current dmn_threshold {dmn} (hysteresis invariant)",
            )
    elif tunable == "dmn_threshold":
        cen = float(registry.get("mode_switcher", "cen_threshold"))
        if float(new_value) >= cen:
            raise ChangeRejected(
                f"dmn_threshold {new_value} must remain strictly less than "
                f"current cen_threshold {cen} (hysteresis invariant)",
            )
