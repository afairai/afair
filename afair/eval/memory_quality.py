"""End-to-end quality gate for answers and living syntheses.

Retrieval metrics only say whether a useful record appeared in a ranked list.
This evaluator scores the product outcome: whether the final understanding is
current, supported, conflict-aware, consistent across clients, and willing to
abstain when the vault has no answer.

The fixture uses stable claim and evidence identifiers. Public CI can score
deterministically without an LLM. A private vault replay adapter can produce
the same shape after using a judge to map free prose onto the fixture's claim
identifiers. Private source material never needs to enter this repository.
"""

from __future__ import annotations

from itertools import combinations

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    id: str
    current: bool = True


class OutputClaim(BaseModel):
    id: str
    citations: list[str] = []
    mode: str = "fact"


class MemoryOutput(BaseModel):
    producer: str
    surface: str = "answer"
    claims: list[OutputClaim] = []
    abstained: bool = False
    conflict_disclosed: bool = False


class MemoryQualityCase(BaseModel):
    id: str
    family: str
    evidence: list[Evidence]
    expected_claims: list[str] = []
    forbidden_claims: list[str] = []
    claim_support: dict[str, list[str]] = {}
    answer_known: bool = True
    requires_conflict_disclosure: bool = False
    outputs: list[MemoryOutput] = Field(min_length=1)


class CaseQuality(BaseModel):
    id: str
    family: str
    truth_precision: float
    current_recall: float
    stale_exclusion: float
    citation_coverage: float
    citation_validity: float
    conflict_honesty: float
    abstention_accuracy: float
    cross_tool_consistency: float


class MemoryQualityReport(BaseModel):
    total_cases: int
    metrics: dict[str, float]
    cases: list[CaseQuality]


class MemoryQualityGate(BaseModel):
    minimums: dict[str, float] = {
        "truth_precision": 1.0,
        "current_recall": 1.0,
        "stale_exclusion": 1.0,
        "citation_coverage": 1.0,
        "citation_validity": 1.0,
        "conflict_honesty": 1.0,
        "abstention_accuracy": 1.0,
        "cross_tool_consistency": 0.9,
    }


class MemoryGateResult(BaseModel):
    passed: bool
    failures: list[str]


DEFAULT_MEMORY_GATE = MemoryQualityGate()


def score_memory_case(case: MemoryQualityCase) -> CaseQuality:
    """Score one case across every output surface and connected client."""

    evidence_ids = {item.id for item in case.evidence}
    current_evidence_ids = {item.id for item in case.evidence if item.current}
    expected = set(case.expected_claims)
    forbidden = set(case.forbidden_claims)

    truth_scores: list[float] = []
    recall_scores: list[float] = []
    stale_scores: list[float] = []
    coverage_scores: list[float] = []
    validity_scores: list[float] = []
    conflict_scores: list[float] = []
    abstention_scores: list[float] = []
    claim_sets: list[set[str]] = []

    for output in case.outputs:
        factual = [claim for claim in output.claims if claim.mode == "fact"]
        output_ids = {claim.id for claim in factual}
        claim_sets.append(output_ids)

        supported = [
            claim
            for claim in factual
            if claim.id in case.claim_support
            and bool(set(claim.citations) & set(case.claim_support[claim.id]))
        ]
        truth_scores.append(len(supported) / len(factual) if factual else 1.0)
        recall_scores.append(len(output_ids & expected) / len(expected) if expected else 1.0)
        stale_scores.append(1.0 if not output_ids & forbidden else 0.0)
        coverage_scores.append(
            sum(bool(claim.citations) for claim in factual) / len(factual) if factual else 1.0
        )

        cited = [citation for claim in factual for citation in claim.citations]
        valid = [citation for citation in cited if citation in evidence_ids]
        current = [citation for citation in cited if citation in current_evidence_ids]
        validity_scores.append(len(valid) / len(cited) if cited else 1.0)
        if cited and len(current) != len(cited):
            stale_scores[-1] = 0.0

        conflict_scores.append(
            1.0 if not case.requires_conflict_disclosure or output.conflict_disclosed else 0.0
        )
        if case.answer_known:
            abstention_scores.append(1.0 if not output.abstained else 0.0)
        else:
            abstention_scores.append(1.0 if output.abstained and not factual else 0.0)

    consistency = _mean_pairwise_jaccard(claim_sets)
    return CaseQuality(
        id=case.id,
        family=case.family,
        truth_precision=_mean(truth_scores),
        current_recall=_mean(recall_scores),
        stale_exclusion=_mean(stale_scores),
        citation_coverage=_mean(coverage_scores),
        citation_validity=_mean(validity_scores),
        conflict_honesty=_mean(conflict_scores),
        abstention_accuracy=_mean(abstention_scores),
        cross_tool_consistency=consistency,
    )


def run_memory_quality(cases: list[MemoryQualityCase]) -> MemoryQualityReport:
    scored = [score_memory_case(case) for case in cases]
    metric_names = tuple(MemoryQualityGate().minimums)
    metrics = {name: _mean([getattr(case, name) for case in scored]) for name in metric_names}
    return MemoryQualityReport(total_cases=len(scored), metrics=metrics, cases=scored)


def evaluate_memory_gate(
    report: MemoryQualityReport,
    gate: MemoryQualityGate = DEFAULT_MEMORY_GATE,
) -> MemoryGateResult:
    failures = [
        f"{name} {report.metrics.get(name, 0.0):.3f} < {minimum:.3f}"
        for name, minimum in gate.minimums.items()
        if report.metrics.get(name, 0.0) < minimum
    ]
    return MemoryGateResult(passed=not failures, failures=failures)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 1.0


def _mean_pairwise_jaccard(sets: list[set[str]]) -> float:
    pairs = list(combinations(sets, 2))
    if not pairs:
        return 1.0
    scores: list[float] = []
    for left, right in pairs:
        union = left | right
        scores.append(len(left & right) / len(union) if union else 1.0)
    return _mean(scores)
