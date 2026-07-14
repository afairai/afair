"""End-to-end memory outcome evaluation."""

from __future__ import annotations

import json
from pathlib import Path

from afair.eval.memory_quality import (
    MemoryOutput,
    MemoryQualityCase,
    OutputClaim,
    evaluate_memory_gate,
    run_memory_quality,
    score_memory_case,
)


def _fixture() -> list[MemoryQualityCase]:
    path = Path(__file__).parents[1] / "afair/eval/fixtures/memory_quality.jsonl"
    return [
        MemoryQualityCase.model_validate(json.loads(line)) for line in path.read_text().splitlines()
    ]


def test_public_memory_quality_fixture_passes_full_gate() -> None:
    report = run_memory_quality(_fixture())
    result = evaluate_memory_gate(report)

    assert report.total_cases == 3
    assert result.passed is True
    assert all(value == 1.0 for value in report.metrics.values())


def test_unsupported_claim_fails_truth_and_citation_coverage() -> None:
    case = MemoryQualityCase(
        id="unsupported",
        family="truth",
        evidence=[{"id": "source"}],
        expected_claims=[],
        claim_support={},
        outputs=[MemoryOutput(producer="model", claims=[OutputClaim(id="invented")])],
    )

    score = score_memory_case(case)

    assert score.truth_precision == 0.0
    assert score.citation_coverage == 0.0


def test_stale_claim_or_stale_citation_fails_exclusion() -> None:
    case = MemoryQualityCase(
        id="stale",
        family="temporal-update",
        evidence=[
            {"id": "old", "current": False},
            {"id": "new", "current": True},
        ],
        expected_claims=["current"],
        forbidden_claims=["old"],
        claim_support={"old": ["old"], "current": ["new"]},
        outputs=[
            MemoryOutput(
                producer="model",
                claims=[OutputClaim(id="old", citations=["old"])],
            )
        ],
    )

    assert score_memory_case(case).stale_exclusion == 0.0


def test_cross_tool_divergence_is_measured() -> None:
    case = MemoryQualityCase(
        id="handoff",
        family="cross-tool",
        evidence=[{"id": "a"}, {"id": "b"}],
        expected_claims=["a", "b"],
        claim_support={"a": ["a"], "b": ["b"]},
        outputs=[
            MemoryOutput(producer="claude", claims=[OutputClaim(id="a", citations=["a"])]),
            MemoryOutput(producer="codex", claims=[OutputClaim(id="b", citations=["b"])]),
        ],
    )

    assert score_memory_case(case).cross_tool_consistency == 0.0


def test_unknown_answer_requires_clean_abstention() -> None:
    case = MemoryQualityCase(
        id="unknown",
        family="abstention",
        evidence=[],
        answer_known=False,
        outputs=[
            MemoryOutput(
                producer="model",
                abstained=True,
                claims=[OutputClaim(id="guess", citations=[])],
            )
        ],
    )

    assert score_memory_case(case).abstention_accuracy == 0.0
