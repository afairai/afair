# Recursive Self-Improvement of the Stack Behind MCP Tools

**Date:** 2026-06-03
**Status:** Design locked, implementation in progress.
**Audience:** Gowry; future contributors needing to reason about why self-modification works the way it does in afair.

---

## 0. Current state

What is true right now (start of this work):

- VISION.md §4 I7: "Self-modification is recorded and reversible. I1–I6 are exempt from self-modification." Already a constitutional invariant; not new.
- afair.ai marketing site `/why` (live since 2026-06-01) carries a methodology section that promises: *"The interface is frozen. The brain underneath isn't."* This document closes the gap between the promise and the implementation.
- The cold-path workers exist: extractor, salience, mode-switcher, entity_canonicalizer, conflict_resolver, consolidator, pruner. Each has hard-coded thresholds and prompt strings inside the worker module.
- `pipeline_events` table exists (Phase 0.5 observability foundation) — partially populated, lifecycle markers per event.
- No tuner. No replay infrastructure. No self-modification log. No invariant guard module.

---

## 1. What "recursive self-improvement" means in this codebase

Three intensity levels, each a quantum leap more risk:

### Level 1 — Tuning *(committed for this build)*

Numeric scalars and prompt variants drift toward better. Worker code itself does not change.

Examples:
- Salience-worker component weights (entity_density, link_density, has_conflict, type_hint_bump, is_compound, recency)
- Mode-switcher hysteresis thresholds (CEN ≥ 8.0, DMN ≤ 4.0 cumulative)
- Surprise-score context window (default 20, env-overridable)
- Extractor prompt variants per `kind` (text vs PDF vs audio vs vision)
- Entity-canonicalizer LLM escalation threshold (Haiku vs Sonnet)
- Conflict-resolver detect threshold
- Consolidator salience cutoff

### Level 2 — Strategy *(roadmap)*

Per-situation algorithm selection. A worker with multiple algorithms picks the one with the best signal for the event-mix it sees.

Example: consolidator could have *clustering*, *chronological*, *entity-graph* strategies. The system learns which works best on which event density.

### Level 3 — Authoring *(roadmap, far future)*

The system writes new worker code itself. Observes a gap (no worker covers the relationship between embeddings and entity edges), generates a Python module, deploys to shadow, validates, promotes via human-approval-gate.

**Gated by human approval at promotion-time** even when Level 1 runs fully autonomous. Code-writing has a different risk profile than parameter-tuning.

---

## 2. Forcing signal — what tells us a variant is actually better

Without a hard signal, self-improvement is drift with good PR. The system collects four classes of signal:

### 2.1 Explicit user-correction signals

These travel through existing MCP verbs with new optional arguments. **No new tools** (I1 constraint).

- `remember(invalidates=[hash])` — existing. The previous event was wrong or outdated.
- `recall(feedback={useful_event_ids, not_useful_event_ids, missing_topic})` — NEW optional arg on existing tool. The AI client reports which prior hits helped after seeing them.
- `recall(query=...)` immediately followed by `remember(content=...)` where the remembered content is similar to what the user expected to find but didn't → soft miss-signal (implicit, no API change).

### 2.2 Implicit user-behavior signals

These need no API change; they read existing traffic.

- `recall(query=X)` → soon after, `recall(by_id=hit, full_payload=True)` = soft endorsement of that hit.
- Short-gap recall sequences on the same topic = retry pattern = first attempt didn't satisfy.
- `observe(action=...)` referencing an event that was NOT returned by a recent recall = we missed.
- `remember(type_hint=X)` where X contradicts the extractor's prior type detection on the same hash = extractor erred.

### 2.3 System-internal calibration signals

No user action involved. Pure self-observation.

- Surprise score predicted "high", recall pattern shows it was never opened = score calibration off.
- Consolidation output never recalled = wasted compute.
- Embedding distance vs entity edges disagree systematically = one of them is lying.
- Extractor produces low-confidence summaries on certain `kind` types = prompt needs work for that kind.

### 2.4 Cost signals

Always available. Used as a tie-breaker, not a primary signal.

- LLM tokens per extraction.
- Cumulative cost per useful_recall (cost ÷ hits-marked-useful).

---

## 3. Validation architecture — solving the low-traffic problem

A solo-user vault produces too little traffic for online A/B statistics. Promotion decisions are made via **three signals in tandem with a time-weighted shift**:

### 3.1 Invariant guards *(hard floor — always required)*

Per-worker assertion suites. A variant must pass ALL invariants regardless of any other signal.

Examples:
- Extractor: every output must parse as valid JSON matching the contract schema; salient_facts must be ≥ 1 item; summary non-empty.
- Entity-canonicalizer: must not split entities sharing canonical name; must not merge entities with different canonical names.
- Salience: output must be in [0, 1].
- Mode-switcher: only emits CEN or DMN, never invalid mode strings.

**A variant that fails an invariant is rejected before any other validation runs.** This is the hard floor against degradation.

### 3.2 LLM-as-Judge on replay *(scales fast, day-1 primary signal)*

The variant runs on the last N events from the actual vault. For each pair `(current_output, variant_output)`, an LLM judge picks the winner.

Properties:
- Multi-vendor: at least three of {Anthropic Sonnet, OpenAI GPT-5, Gemini 2.5 Pro} via litellm. Majority vote required. I5 vendor-neutrality respected.
- Judge prompt human-authored, frozen per tuner version. Never recursively-tuned by the same loop.
- Token cost bounded: per tuner run, max $5 USD across all judge calls; tuner aborts if budget exceeded.
- Threshold: variant must win ≥ 70% of pairs (not 51%) to promote. Conservative to suppress judge noise.

Risk: the judge becomes ground truth. If the judge is biased, we optimize toward the judge not toward usefulness. Mitigations:
- Multi-vendor majority
- Watch the divergence between judge-says-better and real-feedback-says-better. When real signal accumulates and disagrees with judge, the judge prompt is flagged for human review.

### 3.3 Real feedback signal *(slow-accumulating, becomes primary over time)*

The `recall.feedback` event stream, plus the implicit behavior signals from §2.2.

Weight shifts over time:

| Phase | Primary | Backup |
| --- | --- | --- |
| Day 1–30 | Judge majority on replay | Invariant guards (hard floor), sparse feedback |
| Day 30–90 | 50/50 judge + feedback events | Invariant guards |
| Day 90+ | Feedback events (real signal) | Judge as sanity check |

After each successful promote, the system records sample-size + signal-source breakdown. The weight shift is data-driven (when feedback event count crosses thresholds), not calendar-driven.

### 3.4 Aggressive auto-rollback

Every promote ships behind a tolerance gate. For the first 50 events after the swap:
- If useful-rate (per §2.1, §2.2) drops > 10% vs prior 50 events on same worker → auto-rollback.
- If invariant assertion fires on any output → immediate rollback.
- Rollback writes a `tuner.rollback` event with reason + the prior parameter value (audit trail).

User notified on every rollback so calibration mistakes surface fast.

---

## 4. Tunable whitelist — what self-mod is allowed to touch

**Explicit allowlist.** Anything not on this list is off-limits to the tuner.

| Worker | Tunable | Type | Bounds |
| --- | --- | --- | --- |
| salience | entity_density weight | float | [0, 1] |
| salience | link_density weight | float | [0, 1] |
| salience | has_conflict weight | float | [0, 1] |
| salience | type_hint_bump weight | float | [0, 1] |
| salience | is_compound weight | float | [0, 1] |
| salience | recency weight | float | [0, 1] |
| salience | weight sum normalization | implicit | must sum to 1.0 |
| mode_switcher | CEN threshold | float | [5.0, 12.0] |
| mode_switcher | DMN threshold | float | [2.0, 6.0] |
| mode_switcher | invariant CEN > DMN | rule | always |
| surprise | context_window | int | [10, 50] |
| extractor | prompt_text:text | string | from prompt-variant pool |
| extractor | prompt_text:pdf | string | from prompt-variant pool |
| extractor | prompt_text:audio | string | from prompt-variant pool |
| extractor | prompt_text:vision | string | from prompt-variant pool |
| entity_canonicalizer | llm_escalation_threshold | float | [0.5, 0.95] |
| consolidator | salience_cutoff | float | [0.3, 0.8] |

Anything not in this table is off-limits. The substrate schema is off-limits. Worker code structure is off-limits. MCP verbs are off-limits.

**Bounded delta per promotion:** any single promote may shift a value by max ±20% of its current value (for floats), or one item in the prompt-variant pool (for strings). Prevents cliff-edge shifts.

---

## 5. Safety bounds and I7 compliance

Every self-modification:

1. **Writes a `tuner.self_mod` event** to the substrate with: worker_name, parameter, prior_value, new_value, evidence (judge_score, feedback_count, replay_size), rationale, timestamp. Origin field set to `agent:tuner`.
2. **Has a rollback hash** — the prior value lives in the event payload. Reverting is a one-shot operation that emits `tuner.rollback`.
3. **Touches only whitelisted parameters.**
4. **Respects bounded delta.**
5. **Cannot violate I1–I6.** MCP verbs frozen. Substrate append-only. Old data re-readable. Single-tenant. Vendor-neutral (judge calls use litellm across 3+ providers).
6. **Recallable.** Calling `recall(query="what has the tuner changed recently?")` surfaces all self_mod events.

**Halt conditions** — auto-pause of all promotion:

- Useful-Work-Rate falls > 15% below 30-day baseline.
- More than 3 rollbacks in any 7-day window.
- Judge cost burns through 24h budget.
- Any invariant fires on production output (not just shadow).

On halt: tuner continues observing, never promotes, sends a notification event so Gowry can investigate.

---

## 6. Architecture — concrete components

### 6.1 New code

| Component | Location | Role |
| --- | --- | --- |
| `Tuner` cold-path worker | `afair/agents/tuner.py` | Orchestrator: every K events OR M hours, generate hypotheses, run replay+judge, promote or skip. |
| `InvariantGuards` module | `afair/agents/guards.py` | Per-worker assertion suites used at promote-time and at runtime (cheap checks on every worker output). |
| `LlmJudge` module | `afair/agents/llm_judge.py` | Multi-vendor LLM judge via litellm. Loads judge prompt, runs N comparisons, returns majority verdict. |
| `Replay` module | `afair/agents/replay.py` | Pulls last N events of a relevant kind, runs through both variants offline, collects outputs. |
| `TunableRegistry` | `afair/agents/tunable_registry.py` | Single source of truth for the whitelist in §4. Workers read parameters from here at runtime. |
| `tuner_state` schema additions | `afair/substrate/tuner_state.py` | Persistent state: current parameter values, hypothesis queue, recent promotes, baselines. Append-only events. |

### 6.2 Modifications to existing code

| File | Change |
| --- | --- |
| `afair/mcp/schemas.py` | Add optional `feedback` parameter shape to `recall` schema. |
| `afair/mcp/handlers.py` | `recall` handler: when feedback present, write a `recall_feedback` event. Continue to return hits normally. |
| `afair/agents/salience.py` | Read weights from TunableRegistry instead of hardcoded literals. |
| `afair/agents/mode_switcher.py` | Read thresholds from TunableRegistry. |
| `afair/agents/extractor.py` | Read prompt from TunableRegistry per kind. |
| `afair/agents/entity_canonicalizer.py` | Read escalation threshold from TunableRegistry. |
| `afair/agents/consolidator.py` | Read salience cutoff from TunableRegistry. |
| `afair/mcp/cold_path.py` | Register `Tuner` as a scheduled worker with traffic-trigger config. |
| MCP tool description in `afair/mcp/descriptions.py` | `recall` description includes feedback usage doc. |
| `afair-web/lib/onboarding-email.ts` | Snippet for new MCP users includes feedback-on-recall instruction. |
| Project `CLAUDE.md` afair section | Add the feedback instruction to the always-loaded snippet. |
| `afair://session-start` resource | Same snippet. |
| VISION.md | New section §7 *"Recursive self-improvement: scope and constraints"* (or update §4 I7 with a forward link). |

---

## 7. Implementation phases

### Phase A — Foundation *(this session)*

1. Plan doc *(this file)*.
2. Optional `feedback` argument on `recall` (MCP schema + handler + event-write).
3. Snippet update across onboarding-email, CLAUDE.md, session-start resource.
4. `TunableRegistry` module. Initial values from each worker's current hardcoded constants.
5. Refactor each whitelisted worker to read from `TunableRegistry` instead of literals.
6. `InvariantGuards` module with assertion suites for: salience, mode_switcher, extractor, entity_canonicalizer.
7. `LlmJudge` module with multi-vendor majority logic via litellm.
8. `Replay` module able to run any worker on a fixed event-list, return outputs.
9. `Tuner` cold-path worker registered, **scheduled but in observation-only mode** — generates hypotheses, runs replay+judge, writes evidence events. **Does NOT promote yet.**
10. Tests for each new module + the modified workers.

### Phase B — First real hypothesis *(this session if time permits, else next)*

1. Pick `surprise.context_window` as the first real promote target. Lowest blast radius, clearest signal, single int.
2. Tuner generates concrete proposed values (e.g., 15 vs 20 vs 25 vs 30).
3. Replay + judge runs for each candidate.
4. Best candidate enters promote with rollback-gate active.
5. Verify rollback fires correctly when degradation is forced (manual test).

### Phase C — Scale out tunables *(next sessions)*

After surprise.context_window has run one full promote-and-monitor cycle without issue:

- Add salience weight tuning (group: all 6 weights together so they normalize).
- Add extractor prompt-variant pool for one kind (text first).
- Add mode_switcher thresholds.

Each gets its own cycle of validation before moving to next.

### Phase D — Level 2: Strategy *(roadmap, no date)*

When Level 1 has produced ≥ 5 successful promotes over ≥ 3 months without rollback storms:

- Consolidator gets multiple strategy implementations (clustering, chronological, entity-graph).
- Tuner learns which strategy fits which event-mix.
- Same validation infrastructure used.

### Phase E — Level 3: Authoring *(roadmap, far)*

Only triggered if there's clear evidence that the existing worker set has a structural gap that parameter-tuning cannot close. **Always human-gated at promote time** regardless of how well Level 1 is running.

Sketch (not committed to detail):
- Authoring worker observes patterns in events that no current worker addresses.
- Generates a Python module spec.
- Human reviews + approves the generated code.
- Code lands in shadow mode for ≥ 7 days with judge + invariant validation.
- Human approves promote.

---

## 8. What this doc does NOT specify

By design:

- The exact judge prompts (those live in `afair/agents/llm_judge_prompts/`, can iterate).
- The exact replay set size per tunable (configured per tunable in TunableRegistry).
- The exact useful-rate calculation formula (encoded in the tuner module; iterate based on early data).
- Worker-implementation details for the new modules (handled in code review at PR time).

---

## 9. Risk register

| Risk | Mitigation |
| --- | --- |
| Judge is systematically biased toward one model's style | Multi-vendor majority; periodic judge-vs-feedback divergence check. |
| Cost spiral (judge runs eat tokens) | Per-run + per-day budget cap; aborts at limit. |
| Promotes degrade silently | Aggressive auto-rollback on useful-rate dip; invariant guards. |
| Tuner thrashes (promote → rollback → promote → rollback) | Cooling-off period per tunable: after a rollback, the parameter is locked for 7 days. |
| Self-mod log explodes the substrate | Tuner events are small (~1 KB each); current volume estimate < 5 events/day. Substrate handles this trivially. |
| MCP-client AI never sends `feedback` arg | Acceptable: implicit + judge signals carry the system. Feedback arg is a bonus, not a requirement. |
| Loss of vendor-neutrality (we depend on one provider for judge) | I5 holds: litellm with explicit multi-provider config; if one fails, fall back to remaining two. |

---

## 10. Constitution diff

This adds to VISION.md §4 I7. Specifically:

> **I7 (extended).** Self-modification is recorded and reversible. Self-modification is implemented by a single dedicated worker (`tuner`). Self-modification is bounded to the explicit tunable whitelist in `analysis/2026-06-03-recursive-self-improvement.md` §4. Promotion is gated by: (a) invariant assertions, (b) multi-vendor LLM judge majority on replay, (c) real feedback signal as it accumulates. Auto-rollback fires on degradation > 10% over the first 50 events post-promote. The MCP verb signatures (I1) and the substrate schema (I2, I3) remain off-limits to the tuner.

---

## 11. Next action

Phase A starts in the next commit. Plan doc lands first as a stable reference; everything implemented from here is a cross-reference back to this file.
