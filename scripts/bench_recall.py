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
    evaluate_gate,
    run_retrieval_quality,
)

_DEFAULT_FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "afair"
    / "eval"
    / "fixtures"
    / "retrieval_quality.jsonl"
)


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
    args = parser.parse_args()

    if not args.fixture.exists():
        print(f"fixture not found: {args.fixture}", file=sys.stderr)
        return 2

    cases = load_cases(args.fixture)
    report = run_retrieval_quality(cases)
    gate = evaluate_gate(report)

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
        print("\nGATE:", "PASS" if gate.passed else "FAIL")

    return 0 if gate.passed else 1


if __name__ == "__main__":
    sys.exit(main())
