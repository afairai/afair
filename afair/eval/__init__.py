"""Recall evaluation — retrieval-quality benchmark + regression gate.

afair's honest answer to "how good is recall, really" (BUILD #2). Two shapes,
both deterministic and offline (shallow/FTS recall, no embedding API), so they
run in CI without keys:

  - retrieval_quality: a labelled fixture of queries tagged with a FAILURE
    CLASS (entity-name, alias, temporal, contradiction-present,
    multi-event-dilution, hard-negative). Scores Hit@1 / Hit@3 / MRR / recall@k
    per family, with per-family hard floors that gate CI and soft families that
    only warn. Shape adapted from GBrain's NamedThingBench (methodology, not
    code).

The label-free regression gate (Jaccard@k + top-1 stability vs a captured
baseline — drift detection before gold labels exist) is the planned follow-up.
"""

from .regression import (
    RegressionReport,
    capture_baseline,
    compare_to_baseline,
    regression_gate_ok,
)
from .retrieval_quality import (
    DEFAULT_GATE,
    BenchCase,
    FamilyScore,
    GateResult,
    QualityReport,
    evaluate_gate,
    run_retrieval_quality,
    score_ranked,
)

__all__ = [
    "DEFAULT_GATE",
    "BenchCase",
    "FamilyScore",
    "GateResult",
    "QualityReport",
    "RegressionReport",
    "capture_baseline",
    "compare_to_baseline",
    "evaluate_gate",
    "regression_gate_ok",
    "run_retrieval_quality",
    "score_ranked",
]
