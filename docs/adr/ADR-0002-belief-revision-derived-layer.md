# ADR-0002 — Belief revision for the derived layer

> **Status:** Accepted
> **Date:** 2026-06-20
> **Audience:** anyone touching the entity graph, the cold-path agents, or recall
> **Relates to:** [ADR-0001](ADR-0001-constitutional-invariants.md) (invariants), VISION.md §4 (I2 append-only, I6 emergent)

## Context

The append-only substrate (your `remember`/`observe`) is trustworthy: it
stores faithfully and is inspectable. The **derived layer** — the entity
graph, edges, and articles the cold path synthesizes — is not, and dogfooding
proved it. Two failure modes showed up in the operator's own vault:

1. **Confabulation.** The extractor inferred relations from mere co-occurrence
   ("Maxime *is tech person in circle of* Sajinth") and the canonicalizer
   trusted them at a flat 0.8 confidence. Fixed in the relation-evidence pass
   (each relation now needs a verbatim quote; see the agents commit).
2. **Inherited garbage.** Worse, the strongest errors were not afair's
   invention. On 2026-05-25 a session migrated the operator's claude.ai
   profile memory into the vault. claude.ai had already confabulated "Maxime =
   a person", "Kennedy Installationen", etc.; the migration copied that in
   verbatim as `remember` events, and the graph faithfully built a confident
   structure on top — amplifying an upstream hallucination.

The root design flaw behind both: **the derived layer auto-applies as truth
with no confirmation step**, violating the project's own rule ("AI suggests,
operator confirms; no silent auto-apply on user data"). A synthesized edge was
"live" the instant it was written.

This ADR records the model we adopt to fix that, and the prior art it rests
on, so the design is grounded rather than improvised (fittingly, given the
failure was ungrounded synthesis).

## Decision

Treat the derived layer as a set of **defeasible beliefs over the immutable
substrate**, governed by classical belief-revision theory, surfaced for
confirmation, and always correctable by append. Five imports, each tied to a
mechanism:

### 1. Epistemic entrenchment → trust tiers (the organizing principle)

AGM belief revision orders beliefs by *entrenchment*; on conflict you retract
the **least entrenched**, changing as little as possible. afair's trust tiers
**are** an entrenchment ordering, computed from a fact's provenance:

```
user_confirmed  >  user_stated  >  agent_derived  >  foreign_import
```

Binding consequences:

- An `agent_derived` edge may **never** override a `user_stated` fact.
- A `foreign_import` fact (memory migrated from another AI) is the **lowest**
  tier and is always review-required — never auto-trusted.
- Conflict resolution and the auto-confirm policy both fall out of this single
  ordering instead of ad-hoc rules.

(Belief revision & TMS overview: Gärdenfors / AGM, see
<https://cse.buffalo.edu/~shapiro/Papers/br-overview.pdf>.)

### 2. Justification-based TMS / defeasible reasoning → source-cascade retraction

In a justification-based truth-maintenance system a belief holds only while its
*justification* does; defeasible reasoning retracts a conclusion when its
supporting premise is **defeated**. Every edge already records the
`source_event_id` that justified it. So invalidating a source event must
**automatically retract every edge it justified** — a one-shot cleanup, not
edge-by-edge.

This mechanism already exists (the cascade worker:
`find_edges_for_source_event` → `write_edge_invalidation`). We keep it and
recognize it as the defeasible-retraction engine. Correcting the 3 contaminated
migration records therefore collapses their entire confabulated subgraph
automatically. (Pollock, *Defeasible Reasoning*; *Evidence Graphs* on
provenance-as-defeasible-argument.)

### 3. Reflection grounding → the evidence requirement (already shipped)

Current LLM-memory research (Reflective Memory Management, 2025) independently
arrives at the same fix: *cite specific episodic evidence for each reflection
so the agent does not produce baseless generalizations.* That is exactly the
verbatim-evidence requirement on relations. Recorded here as the theoretical
basis for a fix already in the code.

### 4. Quarantine + calibrated confidence → the review queue + auto-confirm policy

Knowledge-graph quality work uses human-in-the-loop **quarantine**: suspect
facts are held for curation, the human sees a **calibrated confidence**, and
only the uncertain are queued (so review effort stays small). afair adopts:

- An append-only `edge_reviews` table: each review is a row
  `(edge_id, verdict ∈ {confirm, reject}, reason, reviewed_by, reviewed_at)`.
  Reject also writes the existing `edge_invalidation`.
- An **auto-confirm policy**: an edge that is evidence-grounded, has a crisp
  predicate, clears a confidence floor, and is **not** `foreign_import` is
  auto-trusted; everything else is `proposed` and goes to the queue.
- The current trust state of an edge resolves from: rejected (has an
  invalidation) → `rejected`; has a confirm review → `confirmed`; else the
  auto-confirm policy → `auto_confirmed` | `proposed`.

(KG quality + HITL: triple-trustworthiness scoring; LLM+human KG validation,
2025.)

### 5. Memory reconsolidation → correction-on-recall (the UX)

Neuroscience: a memory becomes **labile (editable) when recalled**, then
re-stabilizes. The design consequence: correction is not a separate chore;
**the moment a fact is recalled is the moment to confirm or correct it.** The
primary correction surface is therefore conversational — when recall surfaces a
belief and the operator reacts ("that's wrong"), the assisting AI proposes the
correction and the operator confirms, applied through `remember(invalidates=)`
(I1-safe; no new verb). A dashboard review queue is the batch surface.

Crucially, reconsolidation in the brain **distorts** (false updates are
well-documented); afair appends, so the original always survives. afair is
strictly better than the brain at exactly the point where the brain lies.

## Consequences

- The derived layer stops being silent truth. New beliefs are `proposed` or
  `auto_confirmed`, never unconditionally authoritative; recall marks the
  difference and never serves a `proposed`/`foreign_import` edge as hard fact.
- The confirm/reject signal is the **ground-truth set the self-improvement
  tuner lacks** (it sits at `promote_enabled=False` for want of one). The
  verify surface produces the data that lets afair improve itself — one build,
  two problems.
- A memory imported from another AI is a **security boundary**, not just a
  quality one: memory-injection attacks (MINJA, 2025, >98% success on GPT-4
  agents) are real, and the claude.ai migration was an accidental injection.
  `foreign_import` entrenchment + mandatory review is the defense, and afair's
  inspectability is itself the mitigation a black box cannot offer.
- This realizes "mnemonic sovereignty" (the 2026 term for afair's thesis) at
  the belief layer: you can see, weigh, confirm, and retract every belief the
  system holds, with full provenance.

## Alternatives considered

- **Keep auto-applying, just improve extraction.** Rejected: better extraction
  raises the floor but a synthesized fact still becomes truth with no operator
  in the loop — the exact rule violation, and no defense against inherited
  garbage.
- **Mutable trust/status column on edges.** Rejected: violates I2 (the entity
  tables carry no-update triggers). Reviews are append-only rows; current state
  is the latest row, same pattern as everything else.
- **Confirm everything (no auto-trust tier).** Rejected: review fatigue. HITL
  research is explicit that queuing only the uncertain is what makes it usable;
  entrenchment + the confidence floor decide what skips the queue.
- **Trust foreign imports like user input.** Rejected: that is what bit us. A
  fact's entrenchment is bounded by the entrenchment of its least-entrenched
  justification.

## References

- Belief revision / TMS overview — <https://cse.buffalo.edu/~shapiro/Papers/br-overview.pdf>
- Pollock, *Defeasible Reasoning* — <https://johnpollock.us/ftp/PAPERS/Defeasible%20Reasoning-Adler&Rips.pdf>
- Evidence Graphs (provenance + defeasible computation) — <https://www.biorxiv.org/content/10.1101/2021.03.29.437561v3.full>
- Triple Trustworthiness Measurement for KGs — <https://arxiv.org/pdf/1809.09414>
- LLM + human-in-the-loop KG validation (2025) — <https://www.sciencedirect.com/science/article/pii/S030645732500086X>
- Memory for Autonomous LLM Agents (survey) — <https://arxiv.org/html/2603.07670v1>
- Security of LLM Long-Term Memory: Toward Mnemonic Sovereignty (2026) — <https://arxiv.org/html/2604.16548v1>
- MemMachine: Ground-Truth-Preserving Memory — <https://arxiv.org/pdf/2604.04853>
- Memory reconsolidation (overview) — <https://www.sciencedirect.com/topics/neuroscience/memory-reconsolidation>
- Neurobiology of memory updating/editing — <https://www.frontiersin.org/journals/systems-neuroscience/articles/10.3389/fnsys.2023.1103770/full>
