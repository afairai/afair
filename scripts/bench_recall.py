#!/usr/bin/env python
"""Run the recall retrieval-quality benchmark and apply the CI gate.

    uv run python scripts/bench_recall.py            # default fixture
    uv run python scripts/bench_recall.py --json     # machine-readable report

Exit codes: 0 = gate passed, 1 = gate failed (hard floor miss or hard-negative
leak), 2 = usage error. Deterministic + offline (shallow/FTS recall), so it
runs in CI without API keys.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from afair.eval import (
    BenchCase,
    capture_baseline,
    compare_to_baseline,
    evaluate_gate,
    regression_gate_ok,
    run_retrieval_quality,
)

_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "afair" / "eval" / "fixtures"
_DEFAULT_FIXTURE = _FIXTURE_DIR / "retrieval_quality.jsonl"
_BASELINE = _FIXTURE_DIR / "regression_baseline.json"


def load_cases(path: Path) -> list[BenchCase]:
    cases: list[BenchCase] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            cases.append(BenchCase.model_validate_json(line))
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description="Recall retrieval-quality benchmark + gate")
    parser.add_argument("--fixture", type=Path, default=_DEFAULT_FIXTURE)
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    parser.add_argument(
        "--capture",
        action="store_true",
        help="re-freeze the label-free regression baseline from current recall",
    )
    args = parser.parse_args()

    if not args.fixture.exists():
        print(f"fixture not found: {args.fixture}", file=sys.stderr)
        return 2

    cases = load_cases(args.fixture)

    if args.capture:
        baseline = capture_baseline(cases)
        _BASELINE.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
        print(f"baseline re-frozen: {len(baseline)} cases → {_BASELINE}")
        return 0

    report = run_retrieval_quality(cases)
    gate = evaluate_gate(report)

    # Label-free regression check against the frozen baseline.
    reg_passed, reg_reasons = True, []
    if _BASELINE.exists():
        reg = compare_to_baseline(cases, json.loads(_BASELINE.read_text(encoding="utf-8")))
        reg_passed, reg_reasons = regression_gate_ok(reg)

    if args.json:
        print(
            json.dumps(
                {
                    "report": report.model_dump(),
                    "gate": {
                        "passed": gate.passed,
                        "failures": gate.failures,
                        "warnings": gate.warnings,
                    },
                },
                indent=2,
            )
        )
    else:
        print(f"cases: {report.total_cases}  forbidden_leaks: {report.forbidden_leaks}\n")
        for fam, m in sorted(report.families.items()):
            print(
                f"  {fam:<22} n={int(m['n'])}  "
                f"hit@1={m['hit_at_1']:.2f}  hit@3={m['hit_at_3']:.2f}  "
                f"mrr={m['mrr']:.2f}  recall@k={m['recall_at_k']:.2f}"
            )
        print()
        for w in gate.warnings:
            print(f"  WARN  {w}")
        for f in gate.failures:
            print(f"  FAIL  {f}")
        for r in reg_reasons:
            print(f"  DRIFT {r}")
        print("\nQUALITY GATE:", "PASS" if gate.passed else "FAIL")
        print("REGRESSION GATE:", "PASS" if reg_passed else "FAIL")

    return 0 if (gate.passed and reg_passed) else 1


if __name__ == "__main__":
    sys.exit(main())
