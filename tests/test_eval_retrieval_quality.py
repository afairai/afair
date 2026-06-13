"""Recall retrieval-quality benchmark (BUILD #2).

Unit-tests the pure metric scorer, then runs the real harness over the shipped
fixture and asserts the gate passes — so the benchmark itself is CI-wired and a
recall regression (e.g. a future change that breaks entity-name lookup) fails
the build.
"""

from __future__ import annotations

from pathlib import Path

from afair.eval import (
    BenchCase,
    evaluate_gate,
    run_retrieval_quality,
    score_ranked,
)

_FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "afair"
    / "eval"
    / "fixtures"
    / "retrieval_quality.jsonl"
)


# ── pure metric scorer ──────────────────────────────────────────────────────


def test_score_ranked_perfect_top1() -> None:
    h1, h3, mrr, rk, leak = score_ranked(["a", "b", "c"], {"a"}, set(), 10)
    assert (h1, h3, mrr, rk, leak) == (1.0, 1.0, 1.0, 1.0, False)


def test_score_ranked_relevant_at_rank_3() -> None:
    h1, h3, mrr, _rk, _leak = score_ranked(["x", "y", "a"], {"a"}, set(), 10)
    assert h1 == 0.0
    assert h3 == 1.0
    assert mrr == 1.0 / 3


def test_score_ranked_recall_counts_all_relevant_in_top_k() -> None:
    _, _, _, rk, _ = score_ranked(["a", "x", "b", "y"], {"a", "b", "c"}, set(), 10)
    assert rk == 2 / 3  # a and b found, c missing


def test_score_ranked_detects_forbidden_leak() -> None:
    _, _, _, _, leak = score_ranked(["a", "bad"], {"a"}, {"bad"}, 10)
    assert leak is True
    # ...but only inside top-k.
    _, _, _, _, leak_outside = score_ranked(["a", "x", "y", "bad"], {"a"}, {"bad"}, 3)
    assert leak_outside is False


# ── live harness over the shipped fixture ───────────────────────────────────


def _load() -> list[BenchCase]:
    return [
        BenchCase.model_validate_json(line)
        for line in _FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_fixture_loads_and_covers_each_family() -> None:
    cases = _load()
    assert len(cases) >= 6
    families = {c.family for c in cases}
    # the hard-gated families must be present, plus a hard-negative case
    assert {"entity-name", "alias", "hard-negative"} <= families


def test_shipped_fixture_passes_the_gate() -> None:
    """The load-bearing assertion: real recall over the fixture clears the
    hard floors (entity-name hit@1, alias hit@3) and leaks no forbidden hit.
    A recall regression breaks this."""
    report = run_retrieval_quality(_load())
    result = evaluate_gate(report)
    assert result.passed, f"gate failed: {result.failures}"
    assert report.forbidden_leaks == 0


def test_entity_name_lookup_is_near_perfect() -> None:
    report = run_retrieval_quality(_load())
    assert report.families["entity-name"]["hit_at_1"] >= 0.9
