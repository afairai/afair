"""Label-free recall regression gate (BUILD #2 step 2).

Compares current recall ranking to a frozen baseline via Jaccard@k + top-1
stability — catches drift before gold labels exist. The load-bearing test
asserts current recall still matches the committed baseline (so a future change
that reorders recall fails CI); plus unit tests of the drift math.
"""

from __future__ import annotations

import json
from pathlib import Path

from afair.eval import (
    BenchCase,
    capture_baseline,
    compare_to_baseline,
    regression_gate_ok,
)
from afair.eval.regression import _jaccard

_DIR = Path(__file__).resolve().parent.parent / "afair" / "eval" / "fixtures"
_FIXTURE = _DIR / "retrieval_quality.jsonl"
_BASELINE = _DIR / "regression_baseline.json"


def _cases() -> list[BenchCase]:
    return [
        BenchCase.model_validate_json(line)
        for line in _FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_jaccard_math() -> None:
    assert _jaccard(["a", "b"], ["a", "b"], 10) == 1.0
    assert _jaccard(["a", "b"], ["a", "c"], 10) == 1 / 3  # {a,b} vs {a,c}
    assert _jaccard([], [], 10) == 1.0


def test_current_recall_matches_committed_baseline() -> None:
    """The regression guard: recall over the fixture must still produce the
    frozen ranking. If this fails, recall ordering drifted — intended or not,
    it needs a baseline refresh + review (scripts/bench_recall.py --capture)."""
    baseline = json.loads(_BASELINE.read_text(encoding="utf-8"))
    report = compare_to_baseline(_cases(), baseline)
    passed, reasons = regression_gate_ok(report)
    assert passed, f"recall drifted from baseline: {reasons}"
    assert report.mean_jaccard_at_k == 1.0
    assert report.top1_stability == 1.0


def test_gate_flags_a_perturbed_baseline() -> None:
    """Sanity: a baseline whose top-1 differs is detected as drift.

    Perturb every case's top-1 (not just one) so the check is robust to fixture
    size — a single-case corruption can dilute below the 0.9 gate floor as the
    fixture grows, which would silently stop testing the gate.
    """
    cases = _cases()
    baseline = capture_baseline(cases)
    for key in baseline:
        baseline[key] = ["__not_a_real_tag__", *baseline[key]]
    report = compare_to_baseline(cases, baseline)
    passed, reasons = regression_gate_ok(report)
    assert not passed
    assert report.top1_stability == 0.0
    assert reasons
