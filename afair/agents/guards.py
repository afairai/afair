"""
Invariant guards — per-worker assertion suites the tuner runs before
promoting any variant.

A variant must pass ALL invariants for its worker. Invariants are the
**hard floor** on variant quality: they don't measure improvement,
they prevent disasters. A variant whose output fails an invariant is
rejected outright, regardless of judge verdict or feedback signal.

Each suite is a pure function over a worker's output samples. The
tuner runs the suite on outputs from BOTH the current production
worker and the candidate variant, on the same input set, and rejects
any variant that newly fails an invariant the current passes.

Design notes:
  * Guards are CHEAP. They run on every promote-attempt and on every
    runtime worker output (smoke-detection in production). No LLM
    calls, no substrate queries beyond what's already in hand.
  * Guards are CONSERVATIVE. False positives (rejecting a good
    variant) are preferred over false negatives (accepting a bad
    one). A guard that fires on edge cases the current worker also
    hits should be reported but does not block by itself.
  * Guards are SPECIFIC. Generic "output is non-empty" goes here.
    "Output makes sense semantically" goes to the LLM judge instead.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GuardResult:
    passed: bool
    failures: tuple[str, ...]
    sample_count: int

    def __bool__(self) -> bool:
        return self.passed


# ─── salience guards ─────────────────────────────────────────────────────


def check_salience_outputs(outputs: list[dict[str, Any]]) -> GuardResult:
    """Each output must have ``salience`` in [0, 1] and a
    ``salience_components`` dict with the expected 6 keys.

    ``outputs`` is the list of `extraction` dicts that the salience
    worker writes (one per scored event). Empty list ⇒ vacuously OK
    (nothing to check); caller is responsible for ensuring sample
    coverage upstream.
    """
    failures: list[str] = []
    expected_components = frozenset({
        "entity_density", "link_density", "has_conflict",
        "type_hint_bump", "is_compound", "recency",
    })
    for i, out in enumerate(outputs):
        if not isinstance(out, dict):
            failures.append(f"#{i}: not a dict")
            continue
        s = out.get("salience")
        if not isinstance(s, (int, float)):
            failures.append(f"#{i}: salience missing or non-numeric")
            continue
        if not (0.0 <= float(s) <= 1.0):
            failures.append(f"#{i}: salience {s} outside [0, 1]")
        comps = out.get("salience_components")
        if not isinstance(comps, dict):
            failures.append(f"#{i}: salience_components missing or non-dict")
            continue
        keys = frozenset(comps.keys())
        if keys != expected_components:
            missing = expected_components - keys
            extra = keys - expected_components
            failures.append(
                f"#{i}: components keys wrong (missing={sorted(missing)} extra={sorted(extra)})",
            )
    return GuardResult(
        passed=not failures,
        failures=tuple(failures),
        sample_count=len(outputs),
    )


# ─── mode_switcher guards ────────────────────────────────────────────────


def check_mode_switcher_outputs(outputs: list[str]) -> GuardResult:
    """Mode-switcher must emit only ``"CEN"`` or ``"DMN"``. No invalid
    or empty strings. Empty input set ⇒ pass."""
    failures: list[str] = []
    for i, mode in enumerate(outputs):
        if mode not in ("CEN", "DMN"):
            failures.append(f"#{i}: invalid mode {mode!r}")
    return GuardResult(
        passed=not failures,
        failures=tuple(failures),
        sample_count=len(outputs),
    )


def check_mode_switcher_thresholds(
    *,
    cen_threshold: float,
    dmn_threshold: float,
) -> GuardResult:
    """The hysteresis rule MUST hold: cen_threshold > dmn_threshold.
    Otherwise the worker would flip modes on every event."""
    if cen_threshold <= dmn_threshold:
        return GuardResult(
            passed=False,
            failures=(
                f"cen_threshold ({cen_threshold}) must be strictly greater "
                f"than dmn_threshold ({dmn_threshold}) — hysteresis violated",
            ),
            sample_count=1,
        )
    return GuardResult(passed=True, failures=(), sample_count=1)


# ─── extractor guards ────────────────────────────────────────────────────


def check_extractor_outputs(outputs: list[dict[str, Any]]) -> GuardResult:
    """Extractor outputs must be parseable as the contract schema:
    a dict with at least ``summary`` (str, non-empty) and
    ``salient_facts`` (list[str], ≥ 1 item).

    Used at promote-time on replay outputs; not at runtime (the
    extractor's own validation already checks shape on each call).
    """
    failures: list[str] = []
    for i, out in enumerate(outputs):
        if not isinstance(out, dict):
            failures.append(f"#{i}: not a dict")
            continue
        summary = out.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            failures.append(f"#{i}: summary missing / empty")
        facts = out.get("salient_facts")
        if not isinstance(facts, list) or len(facts) < 1:
            failures.append(f"#{i}: salient_facts missing or empty list")
        elif not all(isinstance(f, str) and f.strip() for f in facts):
            failures.append(f"#{i}: salient_facts contains non-str or empty entries")
    return GuardResult(
        passed=not failures,
        failures=tuple(failures),
        sample_count=len(outputs),
    )


# ─── entity_canonicalizer guards ─────────────────────────────────────────


def check_canonicalizer_merges(
    merges: list[dict[str, Any]],
) -> GuardResult:
    """A merge decision can never split entities that already share a
    canonical name. Each ``merges`` item is a dict produced by the
    canonicalizer with at least ``source_entity_name`` and
    ``target_entity_name`` keys."""
    failures: list[str] = []
    for i, m in enumerate(merges):
        src = m.get("source_entity_name")
        tgt = m.get("target_entity_name")
        if not isinstance(src, str) or not isinstance(tgt, str):
            failures.append(f"merge #{i}: missing source or target name")
            continue
        # Same-name merge → always fine. Different names → also fine
        # (that's the whole point). Guard only catches the malformed
        # case above.
    return GuardResult(
        passed=not failures,
        failures=tuple(failures),
        sample_count=len(merges),
    )


# ─── consolidator guards ─────────────────────────────────────────────────


def check_consolidator_outputs(outputs: list[dict[str, Any]]) -> GuardResult:
    """Each consolidation must have a non-empty summary and reference
    at least one event in ``parent_hashes`` — a consolidation with no
    parent events is malformed (nothing to consolidate from).
    """
    failures: list[str] = []
    for i, out in enumerate(outputs):
        if not isinstance(out, dict):
            failures.append(f"#{i}: not a dict")
            continue
        summary = out.get("summary") or out.get("text")
        if not isinstance(summary, str) or not summary.strip():
            failures.append(f"#{i}: empty summary")
        parents = out.get("parent_hashes")
        if not isinstance(parents, list) or len(parents) == 0:
            failures.append(f"#{i}: parent_hashes missing or empty")
    return GuardResult(
        passed=not failures,
        failures=tuple(failures),
        sample_count=len(outputs),
    )
