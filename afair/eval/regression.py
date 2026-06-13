"""Label-free recall regression gate.

The retrieval-quality benchmark needs gold labels. This one does not: it
captures a BASELINE of recall's ranked output for a fixed corpus + query set,
then on every later run compares the new ranking to the baseline via Jaccard@k
(did the top-k set change?) and top-1 stability (did the #1 result change?). A
drift below threshold fails CI — so a refactor that silently reorders recall is
caught even before anyone has labelled what the *right* answer is.

Reuses the retrieval-quality fixture as the corpus (same seeds + queries), but
ignores its labels — only the ranking matters here.
"""

from __future__ import annotations

from pydantic import BaseModel

from .retrieval_quality import BenchCase, _run_one_case_ranked


class RegressionReport(BaseModel):
    n: int
    mean_jaccard_at_k: float
    top1_stability: float
    drifted: list[str]  # case keys whose top-1 changed


def _case_key(case: BenchCase, idx: int) -> str:
    return f"{idx}:{case.family}:{case.query}"


def capture_baseline(cases: list[BenchCase]) -> dict[str, list[str]]:
    """Run each case and record its ranked tag list — the baseline to freeze."""
    return {_case_key(c, i): _run_one_case_ranked(c) for i, c in enumerate(cases)}


def _jaccard(a: list[str], b: list[str], k: int) -> float:
    sa, sb = set(a[:k]), set(b[:k])
    if not sa and not sb:
        return 1.0
    union = sa | sb
    return len(sa & sb) / len(union) if union else 1.0


def compare_to_baseline(
    cases: list[BenchCase], baseline: dict[str, list[str]], *, k: int = 10
) -> RegressionReport:
    """Compare current ranking to the frozen baseline."""
    jaccards: list[float] = []
    top1_same = 0
    counted = 0
    drifted: list[str] = []

    for i, case in enumerate(cases):
        key = _case_key(case, i)
        if key not in baseline:
            continue  # a new case with no baseline yet — skip (don't fail)
        counted += 1
        now = _run_one_case_ranked(case)
        base = baseline[key]
        jaccards.append(_jaccard(now, base, k))
        now_top1 = now[0] if now else None
        base_top1 = base[0] if base else None
        if now_top1 == base_top1:
            top1_same += 1
        else:
            drifted.append(key)

    return RegressionReport(
        n=counted,
        mean_jaccard_at_k=(sum(jaccards) / len(jaccards)) if jaccards else 1.0,
        top1_stability=(top1_same / counted) if counted else 1.0,
        drifted=drifted,
    )


def regression_gate_ok(
    report: RegressionReport, *, min_jaccard: float = 0.9, min_top1: float = 0.9
) -> tuple[bool, list[str]]:
    """Apply thresholds. Returns (passed, reasons)."""
    reasons: list[str] = []
    if report.mean_jaccard_at_k < min_jaccard:
        reasons.append(
            f"mean Jaccard@k {report.mean_jaccard_at_k:.3f} < {min_jaccard} "
            "(top-k sets drifted from baseline)"
        )
    if report.top1_stability < min_top1:
        reasons.append(
            f"top-1 stability {report.top1_stability:.3f} < {min_top1} "
            f"(#1 result changed for: {', '.join(report.drifted)})"
        )
    return (not reasons, reasons)
