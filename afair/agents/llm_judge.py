"""
LLM-as-Judge: pairwise comparison of worker variants for the
self-improvement tuner.

The tuner gives the judge a sample of (input, current_output,
variant_output) triples. The judge picks a winner per triple. The
tuner aggregates: variant wins if its majority share ≥ promotion
threshold (default 0.70, conservative).

Why a judge at all: at solo-user traffic, online A/B requires
weeks-to-months for statistical significance. Offline replay +
judge gives same-day verdicts using real vault data. The trade-off
is "judge becomes ground truth" — defended via:

  1. Multi-vendor majority. At least three of
     {Anthropic, OpenAI, Google} via litellm; majority vote required.
     Suppresses any single vendor's stylistic bias.

  2. Judge prompt is human-authored and version-stamped. NEVER
     tuned by the same self-improvement loop (would create
     recursive bias amplification).

  3. Budget cap. Each tuner run can't burn more than a hard token
     limit; aborts gracefully if exceeded.

  4. Drift watch. When real-traffic feedback signals (from
     RecallFeedback events + implicit behavior) consistently
     contradict judge verdicts, the judge prompt is flagged for
     human review.

This module exposes :func:`judge_pairs` as the entry point. The
tuner imports it; nobody else should.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import structlog

log = structlog.get_logger(__name__)


# Default panel: three vendors that respond well to "compare and pick"
# instructions. Each is a litellm-compatible model string. The tuner
# can override by passing its own panel.
DEFAULT_PANEL: tuple[str, ...] = (
    "anthropic/claude-sonnet-4-5",
    "openai/gpt-5",
    "gemini/gemini-2.5-pro",
)


# Default verdict threshold — variant must win this share to promote.
# Conservative on purpose: at majority-of-3, 0.70 implies ≥ 21 of 30
# pairs go to variant, well above 50/50 noise.
DEFAULT_PROMOTE_THRESHOLD = 0.70


# Per-run cost ceiling. Aborts the tuner cycle if any single judge
# panel exceeds this. Tuned conservative for Phase A.
DEFAULT_TOKEN_BUDGET = 200_000  # ~$2 worst-case at flagship rates


# Frozen judge prompt for v0. The tuner is NEVER allowed to tune
# this string — see module docstring. Bumping this prompt requires
# a code change AND a version stamp.
JUDGE_PROMPT_VERSION = "v0:2026-06-03"

JUDGE_SYSTEM_PROMPT = """You compare two outputs of the same worker
in the afair memory system and pick the one that better fits the
worker's purpose.

You will see:
  • The worker's name and short purpose.
  • The exact input that produced both outputs.
  • Output A (one variant) and Output B (the other variant).
  • The worker's quality criteria.

Pick A, B, or TIE. Briefly state the deciding factor in ≤ 30 words.

Rules:
  • Both outputs already passed structural validity checks. You
    judge SUBSTANTIVE quality — does the output do its job well?
  • A and B are anonymized — you don't know which is the current
    production worker and which is the proposed variant.
  • If both are equally good (or equally weak), pick TIE.
  • Never penalize a different stylistic choice if it's equally
    valid. Only penalize substantive errors or omissions.

Respond with valid JSON:
  {"verdict": "A" | "B" | "TIE", "reason": "..."}
"""


@dataclass(frozen=True)
class JudgePair:
    """One input that fed both variants — judge picks which output is better."""
    input_summary: str
    output_a: str
    output_b: str
    worker_name: str
    worker_purpose: str
    quality_criteria: str


@dataclass(frozen=True)
class JudgeVerdict:
    pair_index: int
    winner: str  # "A" | "B" | "TIE"
    reason: str
    model: str


@dataclass(frozen=True)
class PanelVerdict:
    """Aggregated panel decision for one pair."""
    pair_index: int
    winner: str  # "A" | "B" | "TIE"
    votes: dict[str, int]  # {"A": 2, "B": 1, "TIE": 0}
    reasons: list[str]


@dataclass(frozen=True)
class JudgeReport:
    """Full result of a panel run over all pairs."""
    pair_count: int
    panel: tuple[str, ...]
    pair_verdicts: tuple[PanelVerdict, ...]
    a_wins: int
    b_wins: int
    ties: int
    a_share: float
    b_share: float
    tokens_spent_estimate: int
    aborted: bool
    abort_reason: str | None


# ─── single-judge call (litellm under the hood) ───────────────────────────


def _ask_one_judge(
    pair: JudgePair,
    pair_index: int,
    model: str,
) -> JudgeVerdict | None:
    """Call ONE model on ONE pair. Returns None on any failure.

    Wraps litellm + JSON-mode. Errors are logged + swallowed because
    the panel-majority can tolerate individual judge failures (we
    just have fewer votes). The tuner catches the "no model returned
    a verdict" case at the aggregation layer.
    """
    import litellm

    user_prompt = _format_pair_prompt(pair)
    try:
        completion = litellm.completion(
            model=model,
            messages=[
                {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
            max_tokens=200,
            response_format={"type": "json_object"},
        )
        body = completion["choices"][0]["message"]["content"]
    except Exception as e:
        log.warning("judge.model_failed", model=model, pair_index=pair_index, error=str(e))
        return None

    try:
        import json
        parsed = json.loads(body) if isinstance(body, str) else body
        verdict = str(parsed.get("verdict", "")).upper()
        reason = str(parsed.get("reason", ""))[:300]
    except Exception as e:
        log.warning("judge.parse_failed", model=model, pair_index=pair_index, error=str(e))
        return None

    if verdict not in ("A", "B", "TIE"):
        log.warning("judge.invalid_verdict", model=model, pair_index=pair_index, raw=verdict)
        return None

    return JudgeVerdict(
        pair_index=pair_index,
        winner=verdict,
        reason=reason,
        model=model,
    )


def _format_pair_prompt(pair: JudgePair) -> str:
    return (
        f"Worker: {pair.worker_name}\n"
        f"Purpose: {pair.worker_purpose}\n"
        f"Quality criteria: {pair.quality_criteria}\n\n"
        f"Input:\n```\n{pair.input_summary}\n```\n\n"
        f"Output A:\n```\n{pair.output_a}\n```\n\n"
        f"Output B:\n```\n{pair.output_b}\n```\n"
    )


# ─── panel aggregation ────────────────────────────────────────────────────


def _aggregate(verdicts: list[JudgeVerdict | None], pair_index: int) -> PanelVerdict:
    real = [v for v in verdicts if v is not None]
    counts = Counter(v.winner for v in real)
    a = counts.get("A", 0)
    b = counts.get("B", 0)
    t = counts.get("TIE", 0)
    # Majority wins; on a tie of A and B, fall through to TIE.
    if a > b and a >= 1:
        winner = "A"
    elif b > a and b >= 1:
        winner = "B"
    else:
        winner = "TIE"
    return PanelVerdict(
        pair_index=pair_index,
        winner=winner,
        votes={"A": a, "B": b, "TIE": t},
        reasons=[v.reason for v in real],
    )


# ─── entry point ─────────────────────────────────────────────────────────


def judge_pairs(
    pairs: list[JudgePair],
    *,
    panel: tuple[str, ...] = DEFAULT_PANEL,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
) -> JudgeReport:
    """Run the full judge panel over a list of pairs.

    For each pair, every panel member is asked. The panel-majority
    becomes that pair's verdict. The report aggregates across all
    pairs into share-of-wins for A and B.

    Budget enforcement is conservative — we estimate tokens per call
    at 1500 (1k prompt + 0.5k response) and abort if the cumulative
    estimate would exceed the budget. The tuner is expected to
    react to ``aborted=True`` by giving up on this hypothesis
    rather than partially trusting a half-finished panel run.
    """
    pair_verdicts: list[PanelVerdict] = []
    tokens_estimate = 0
    per_call_estimate = 1500
    aborted = False
    abort_reason: str | None = None

    for i, pair in enumerate(pairs):
        next_cost = per_call_estimate * len(panel)
        if tokens_estimate + next_cost > token_budget:
            aborted = True
            abort_reason = (
                f"token budget {token_budget} would be exceeded "
                f"({tokens_estimate} spent, {next_cost} for next pair)"
            )
            log.warning(
                "judge.budget_abort",
                pairs_completed=i,
                pairs_remaining=len(pairs) - i,
                spent=tokens_estimate,
            )
            break

        verdicts: list[JudgeVerdict | None] = []
        for model in panel:
            verdicts.append(_ask_one_judge(pair, i, model))
        tokens_estimate += per_call_estimate * len(panel)
        pair_verdicts.append(_aggregate(verdicts, i))

    a_wins = sum(1 for v in pair_verdicts if v.winner == "A")
    b_wins = sum(1 for v in pair_verdicts if v.winner == "B")
    ties = sum(1 for v in pair_verdicts if v.winner == "TIE")
    n = max(1, len(pair_verdicts))

    return JudgeReport(
        pair_count=len(pair_verdicts),
        panel=panel,
        pair_verdicts=tuple(pair_verdicts),
        a_wins=a_wins,
        b_wins=b_wins,
        ties=ties,
        a_share=a_wins / n,
        b_share=b_wins / n,
        tokens_spent_estimate=tokens_estimate,
        aborted=aborted,
        abort_reason=abort_reason,
    )
