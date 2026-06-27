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


def test_temporal_queries_now_prefer_the_recent_record() -> None:
    """The gap the benchmark found (temporal hit@1=0) is closed by the
    temporal-intent recency re-rank. Lock it in as a regression guard."""
    report = run_retrieval_quality(_load())
    assert report.families["temporal"]["hit_at_1"] >= 0.9


def test_stale_memories_sink_below_live_ones() -> None:
    """The relevance-decay layer: a passed deadline and a superseded fact must
    not outrank the live memory. These cases score hit@1=0 without decay (the
    stale event matches the query terms more strongly); the decay re-rank flips
    them. Regression guard for P2/P3 of the relevance-decay feature."""
    report = run_retrieval_quality(_load())
    assert report.families["stale-demotion"]["hit_at_1"] >= 0.9


# ── the recency re-rank itself (unit) ───────────────────────────────────────


def test_temporal_intent_detection() -> None:
    from afair.mcp.handlers import _has_temporal_intent

    for q in ["current role", "Maya latest title", "what is X now", "as of today"]:
        assert _has_temporal_intent(q), q
    for q in ["who is Sajinth", "Clario funding", "design system"]:
        assert not _has_temporal_intent(q), q


def test_recency_rerank_orders_newest_first() -> None:
    from datetime import UTC, datetime, timedelta

    from afair.mcp.handlers import _recency_rerank
    from afair.substrate.events import Event

    def _ev(tag: str, age_days: int) -> Event:
        return Event(
            id=tag,
            content_hash=f"sha256:{abs(hash(tag)):064d}"[:71],
            created_at=(datetime.now(UTC) - timedelta(days=age_days)).isoformat(),
            origin="agent",
            kind="remember",
            payload={"content_type": "text", "text": tag},
            schema_version=1,
        )

    # old first in input; recency re-rank must put the newer one on top.
    out = _recency_rerank([_ev("old", 700), _ev("new", 5)])
    assert [e.id for e in out] == ["new", "old"]
