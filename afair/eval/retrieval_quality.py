"""Retrieval-quality benchmark for recall.

A case = a query + a set of seed events to write into a fresh vault + which of
those seeds are the gold-relevant answers + the failure-class family the query
stresses. The harness builds a clean vault per case, runs shallow (FTS) recall,
and scores Hit@1 / Hit@3 / MRR / recall@k. Per-family aggregation + a gate with
hard floors (CI-failing) on the families mapped to known afair risks, and soft
families that only warn.

Deterministic + offline by construction: shallow recall is FTS-only, so there
is no embedding API call and the scores are reproducible in CI.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

# Failure-class families — each names a retrieval failure mode afair cares
# about. Hard families gate CI; soft families warn. Keep this list small and
# every entry tied to a real risk, not vanity coverage.
FAMILIES = (
    "entity-name",  # query is an entity's canonical name
    "alias",  # query uses a known alias / alternate surface form
    "temporal",  # recency-sensitive ("latest", "current")
    "stale-demotion",  # a passed/superseded memory must not outrank the live one
    "contradiction-present",  # relevant record exists amid a conflicting one
    "multi-event-dilution",  # one strong relevant event among many weak ones
    "hard-negative",  # query that must NOT surface a particular event
)


class BenchSeed(BaseModel):
    """One event to seed into the case's vault. ``tag`` is a stable handle the
    case uses to name gold/forbidden answers without knowing content hashes.

    ``age_days`` backdates the event's created_at so temporal/recency behaviour
    can be tested honestly (an "old role" vs a "current role" that were recorded
    at different times, not both at once).

    The optional temporal_* fields seed an ``event_temporal`` record directly
    (no worker, no LLM), so the relevance-decay ranking can be exercised
    deterministically: a one-off past its ``relevance_horizon`` decays, a
    ``superseded`` memory is floored."""

    tag: str
    text: str
    age_days: int = 0
    temporal_class: str | None = None
    event_time: str | None = None
    relevance_horizon: str | None = None
    recurrence_rule: str | None = None
    closure_state: str | None = None
    temporal_confidence: float = 0.9


class BenchCase(BaseModel):
    """One benchmark query.

    ``relevant`` are seed tags that SHOULD rank for the query. ``forbidden``
    are seed tags that must NOT appear in the top-k (the hard-negative guard).
    """

    query: str
    family: str
    seeds: list[BenchSeed]
    relevant: list[str] = []
    forbidden: list[str] = []
    k: int = 10


@dataclass
class CaseScore:
    family: str
    hit_at_1: float
    hit_at_3: float
    mrr: float
    recall_at_k: float
    forbidden_leak: bool  # a forbidden tag appeared in the top-k


# ── metrics (pure) ──────────────────────────────────────────────────────────


def score_ranked(
    ranked: list[str], relevant: set[str], forbidden: set[str], k: int
) -> tuple[float, float, float, float, bool]:
    """Score one ranked list of tags against the gold/forbidden sets.

    Returns (hit@1, hit@3, mrr, recall@k, forbidden_leak). Pure — no I/O — so
    it is unit-testable in isolation.
    """
    hit_at_1 = 1.0 if ranked[:1] and ranked[0] in relevant else 0.0
    hit_at_3 = 1.0 if any(t in relevant for t in ranked[:3]) else 0.0

    mrr = 0.0
    for i, tag in enumerate(ranked, start=1):
        if tag in relevant:
            mrr = 1.0 / i
            break

    top_k = ranked[:k]
    found = len(relevant & set(top_k))
    recall_at_k = found / len(relevant) if relevant else 1.0

    forbidden_leak = bool(forbidden & set(top_k))
    return hit_at_1, hit_at_3, mrr, recall_at_k, forbidden_leak


# ── runner ──────────────────────────────────────────────────────────────────


def _run_one_case_ranked(case: BenchCase) -> list[str]:
    """Build a fresh vault, write the seeds, run shallow recall, return the
    ranked list of seed tags. Shared by the quality scorer and the label-free
    regression gate.

    Imports are local so importing this module never drags in the MCP/server
    stack for callers who only want the pure metrics.
    """
    from datetime import UTC, datetime, timedelta

    from ..mcp import handlers
    from ..mcp.context import ServerContext, clear_context, set_context
    from ..substrate import open_db, write_event, write_event_temporal

    with tempfile.TemporaryDirectory() as tmp:
        db = open_db(Path(tmp))
        sc = ServerContext(
            db=db,
            vault_dir=Path(tmp),
            inline_text_max_bytes=64 * 1024,
            semantic_recall_enabled=False,  # FTS-only → deterministic, offline
        )
        set_context(sc)
        try:
            now = datetime.now(UTC)
            tag_by_event_id: dict[str, str] = {}
            for seed in case.seeds:
                created_at = (
                    (now - timedelta(days=seed.age_days)).isoformat() if seed.age_days else None
                )
                ev = write_event(
                    db,
                    origin="agent",
                    kind="remember",
                    payload={"content_type": "text", "text": seed.text},
                    created_at=created_at,
                )
                tag_by_event_id[ev.id] = seed.tag
                if seed.temporal_class is not None:
                    write_event_temporal(
                        db,
                        event_id=ev.id,
                        event_hash=ev.content_hash,
                        temporal_class=seed.temporal_class,
                        confidence=seed.temporal_confidence,
                        computed_by="temporal:v1",
                        event_time=seed.event_time,
                        relevance_horizon=seed.relevance_horizon,
                        recurrence_rule=seed.recurrence_rule,
                        closure_state=seed.closure_state,
                    )

            result = handlers.recall(query=case.query, depth="shallow", limit=case.k)
            return [tag_by_event_id.get(h.event_id, "") for h in result.hits]
        finally:
            db.close()
            clear_context()


def _run_one_case(case: BenchCase) -> CaseScore:
    """Run a case and score it against its gold/forbidden labels."""
    ranked = _run_one_case_ranked(case)
    h1, h3, mrr, rk, leak = score_ranked(ranked, set(case.relevant), set(case.forbidden), case.k)
    return CaseScore(case.family, h1, h3, mrr, rk, leak)


@dataclass
class FamilyScore:
    family: str
    n: int
    hit_at_1: float
    hit_at_3: float
    mrr: float
    recall_at_k: float
    forbidden_leaks: int


class QualityReport(BaseModel):
    total_cases: int
    families: dict[str, dict[str, float]]  # family → metric → value
    forbidden_leaks: int


def run_retrieval_quality(cases: list[BenchCase]) -> QualityReport:
    """Run every case, aggregate per family."""
    scores = [_run_one_case(c) for c in cases]

    by_family: dict[str, list[CaseScore]] = {}
    for s in scores:
        by_family.setdefault(s.family, []).append(s)

    families: dict[str, dict[str, float]] = {}
    total_leaks = 0
    for fam, fam_scores in by_family.items():
        n = len(fam_scores)
        leaks = sum(1 for s in fam_scores if s.forbidden_leak)
        total_leaks += leaks
        families[fam] = {
            "n": float(n),
            "hit_at_1": sum(s.hit_at_1 for s in fam_scores) / n,
            "hit_at_3": sum(s.hit_at_3 for s in fam_scores) / n,
            "mrr": sum(s.mrr for s in fam_scores) / n,
            "recall_at_k": sum(s.recall_at_k for s in fam_scores) / n,
            "forbidden_leaks": float(leaks),
        }

    return QualityReport(
        total_cases=len(cases),
        families=families,
        forbidden_leaks=total_leaks,
    )


# ── gate ────────────────────────────────────────────────────────────────────


@dataclass
class Gate:
    """Per-family floors. Hard families fail the build; soft families warn.

    Floors start conservative and are tightened as the fixture grows. Any
    forbidden-tag leak fails regardless of family (a precision guard).
    """

    hard: dict[str, dict[str, float]] = field(default_factory=dict)
    soft: tuple[str, ...] = ()


# Hard floors on the families mapped to afair's real risks: direct entity
# lookup must be near-perfect, and a hard-negative must never leak. Everything
# else is a soft warning until the fixture is large enough to set a real floor.
DEFAULT_GATE = Gate(
    hard={
        "entity-name": {"hit_at_1": 0.9},
        "alias": {"hit_at_3": 0.9},
        # Promoted to hard once the temporal-intent recency re-rank landed —
        # "current/latest" queries must surface the newest record.
        "temporal": {"hit_at_1": 0.9},
        # The relevance-decay layer: a passed deadline / superseded fact must
        # not outrank the live memory. Without decay these score hit@1=0.
        "stale-demotion": {"hit_at_1": 0.9},
    },
    soft=("contradiction-present", "multi-event-dilution", "hard-negative"),
)


@dataclass
class GateResult:
    passed: bool
    failures: list[str]
    warnings: list[str]


def evaluate_gate(report: QualityReport, gate: Gate = DEFAULT_GATE) -> GateResult:
    """Apply the gate. Any hard-family floor miss or any forbidden leak fails."""
    failures: list[str] = []
    warnings: list[str] = []

    if report.forbidden_leaks:
        failures.append(
            f"hard-negative leak: {report.forbidden_leaks} forbidden record(s) ranked in top-k"
        )

    for fam, floors in gate.hard.items():
        metrics = report.families.get(fam)
        if metrics is None:
            warnings.append(f"hard family '{fam}' has no cases in the fixture")
            continue
        for metric, floor in floors.items():
            got = metrics.get(metric, 0.0)
            if got < floor:
                failures.append(f"{fam}.{metric} = {got:.3f} < floor {floor:.2f}")

    for fam in gate.soft:
        metrics = report.families.get(fam)
        if metrics and metrics.get("mrr", 1.0) < 0.3:
            warnings.append(f"soft family '{fam}' mrr={metrics['mrr']:.3f} is low")

    return GateResult(passed=not failures, failures=failures, warnings=warnings)
