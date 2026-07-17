# ADR-0009: Operator content correction through append-only supersession

> **Status:** Accepted
> **Date:** 2026-07-17
> **Audience:** anyone touching the correction route, the living synthesis worker, the Memory Mirror serving path, or the dashboard correction UI
> **Relates to:** VISION.md §4 (I1, I2, I3, I4, I7, I8), [ADR-0001](ADR-0001-constitutional-invariants.md) (the erasure boundary), [ADR-0002](ADR-0002-belief-revision-derived-layer.md) (the proposal-decide loop this route deliberately does not use), [ADR-0004](ADR-0004-edge-confidence-model.md) (serve with a caveat, never suppress silently), [ADR-0007](ADR-0007-emergent-living-syntheses.md) (the living syntheses being corrected), [ADR-0008](ADR-0008-operator-conflict-resolution.md) (the actionable Mirror this completes)

## Context

The actionable Memory Mirror (ADR-0008) lets the operator resolve conflicts
and confirm or reject what the pipeline inferred. Both of those act on
things the SYSTEM raised: a conflict exists because two events contradict
each other, and a pending correction exists because a cold-path worker
proposed one. Neither covers the plainest failure the Mirror exposes: a
fact that is simply wrong. A misheard name, a stale claim the vault
captured once and never contradicted, an extraction that landed a wrong
detail into a synthesis. No counter-event exists, so no conflict fires; no
worker proposal exists, so nothing enters the pending queue. The operator
can see the wrong fact on the dashboard and has no way to act on it.

The substrate already has the right primitive. Supersession is the shipped
append-only correction model: `write_invalidation` appends a new
`invalidate` event whose `parent_hashes` names the target, the target stays
byte-identical, and every current-state read excludes invalidated events
while every history read keeps them. The frozen MCP surface exposes the
same model as `remember(invalidates=)`: write the corrected fact, mark the
old one superseded, in one call. What is missing is not a mechanism but a
reachable path: the Mirror shows the wrong fact, and the operator should be
able to correct it where they see it.

One boundary must stay sharp. VISION.md §4 (I2) and ADR-0001 distinguish
two operations that surface language tends to blur:

- **Erasure** is for content that must be GONE: a logged tombstone plus
  crypto-shred, leaving an auditable hole. It exists for legal and safety
  obligations, not for wrong facts.
- **Correction** is supersession: history is kept in full, and only the
  current-state view changes.

This ADR is about correction only. Nothing here deletes user bytes, and no
UI copy may describe any of it as deletion.

There is also an honesty problem specific to syntheses. A living synthesis
(ADR-0007) is derived: a model reads a cluster of source events and writes
a summary with key points. When a key point is wrong, invalidating the
synthesis event does remove it from serving, but the worker will re-derive
a fresh synthesis from the same unchanged sources with the same model
within its cycle, and nothing steers that fresh run away from the prior
wrong claim. Re-derivation can reproduce the error. Any design that
presents "reject the synthesis" as a fix is overclaiming; the design below
does not.

## Decision

Operator content correction ships as a distinct operator-initiated write
route, composed entirely from shipped append-only primitives, in two
flavors: mark a source event wrong (Flavor A), and correct a derived
synthesis honestly (Flavor B). Four parts:

### 1. A distinct route, not a synthetic proposal

Corrections land on a new dashboard-authenticated route,
`POST /internal/correct`. They are deliberately NOT routed through
`decide_correction`.

`decide_correction` is the ADR-0002 loop: the AI proposes, the operator
confirms. Every entry in that queue is a machine judgment awaiting a human
verdict. An operator marking a fact wrong is the opposite shape: the
operator asserts, and there is no proposal to confirm. Forcing this
through the decide path would mean minting a synthetic proposal the moment
the button is pressed and confirming it in the same breath, which inverts
the ADR-0002 semantics (the queue would contain operator assertions
dressed as machine proposals), pollutes every pending-queue view and count
with rows that were never actually pending, and buys nothing, because the
route needs no verdict vocabulary, no proposal lifecycle, and no queue.

The route instead composes the two shipped substrate primitives directly:
`write_invalidation` for supersession and `write_event_with_status` for
new content, the same composition the import route uses. The route itself
runs no SQL; a single substrate function performs the writes on one
connection, content first. Every correction carries full provenance: an
observe event records the action, the target, and the outcome (I7), and
the correction payload records `corrected_by`, so the vault's own record
answers who corrected what, when, and how. Every write is reversible by
construction: an invalidation can be superseded by a later re-validation,
and a suppression can be superseded by a later restore row.

The auth and abuse posture adds no new credential class: the route sits
behind the existing dashboard JWT, which is short-lived and pinned to the
vault's subject (I8, single-tenant, so the only person who can reach it is
the vault's operator), behind the existing `/internal` rate limiter, with
hard input bounds (correction text capped at 20000 bytes, reason and note
at 500 characters), and with the full observe audit trail. Because
everything is append-only and reversible, the worst a compromised session
can do is leave a visible, attributable, undoable trail.

### 2. Flavor A: a source event is wrong

The operator marks a source event wrong, optionally stating what is
actually true. The route writes, in order:

- **A correction event, when correction text is given.** An ordinary
  `remember` event (origin `user`, type hint `operator_correction`) whose
  `parent_hashes` names the corrected event. The parent link matters: it
  places the correction inside the same evidence lineage, so the next
  re-synthesis sees the fix in its cluster rather than as an unrelated
  stray fact. The event enters the normal extraction pipeline like any
  other remember.
- **An `invalidate` event** against the target via `write_invalidation`,
  carrying the correction text or reason. The target is never touched.
- **An observe event** recording the correction (I7).

The path is idempotent: an already-invalidated target skips the duplicate
invalidation and reports it, and a retried correction event deduplicates
on its content hash.

Downstream, everything follows from shipped behavior with no new
machinery: the Mirror marks the source not-current on the next fetch, the
living synthesis worker's eligibility query already excludes invalidated
events, so the affected cluster changes and forces a re-synthesis without
the wrong source (and with the correction event, if written, now inside
it), within the worker's cycle of at most six hours. A cluster that drops
below the minimum evidence size retires its synthesis instead.

### 3. Flavor B: a synthesis is wrong, designed honestly

A synthesis is derived content. The honest core, stated once and reflected
everywhere: re-deriving from unchanged sources with the same model can
REPRODUCE the error, so blind re-synthesis is not a fix. The primary
honest remedies are to correct the source the error came from (Flavor A)
or to suppress the specific wrong claim. The MVP is b2 plus b1; b3 is
named and deferred.

- **b2, suppress a key point (the recommended path).** Precise,
  append-only, no model call. The operator marks one served key point
  wrong, optionally with a note. The route validates that the target is a
  living synthesis and that the point text matches a served key point,
  identified by a digest of its normalized text, because key points carry
  no stable id. It then appends one interpretation row against the
  synthesis, `produced_by = "key_point_review:v1:<point_digest>"`,
  carrying the digest, the point text, the verdict (`suppress` or
  `restore`), the cluster id, the note, and the decision stamp. Latest
  row wins per producer, so a restore is a later row, never a mutation,
  and the path is idempotent when the latest row already carries the
  requested verdict. An observe event records the decision.

  The read path annotates: a suppressed key point is served WITH a
  suppression marker and the operator's note, not dropped. This is the
  ADR-0004 posture (annotate contested material, never suppress it
  silently), it keeps the audit trail visible, and it keeps the reversal
  path obvious. The synthesis payload itself is never rewritten; the
  annotation is projection-only. Because the record carries the cluster
  id, the read path can digest-match a verbatim carry-forward of the same
  wrong point on a future re-synthesis and keep the marker attached.
- **b1, reject a wholesale-wrong synthesis.** Reuses the Flavor A route
  with the synthesis's content hash as the target: the synthesis event is
  invalidated, the Mirror stops serving it immediately, the worker's live
  priors exclude it, and its full-text index row (a regenerable index
  entry, not substrate) is removed. The honest caveat is part of the
  design, not a footnote: a fresh synthesis may form from the same
  sources within the next cycle and may repeat the error. The UI must say
  so and must point the operator at correcting the source instead when a
  specific fact is the problem. Rejection is removal from serving, not a
  guarantee of a better replacement.
- **b3, steer the re-synthesis, deferred.** The durable fix for
  re-derivation is for the synthesis prompt to read the live suppressions
  and instruct the model not to restate operator-rejected claims. That
  changes the LLM contract and injects operator-adjacent text into the
  prompt, where the quoted wrong claims are themselves prior model output
  and must be wrapped as untrusted. It is deferred until that contract
  and injection-safety review is done, and it is the acknowledged answer
  to the digest-matching limitation below.

### 4. Supersession never deletes bytes

Restating the boundary as a rule for this route: `/internal/correct`
never deletes user content. Every path above appends. The single deletion
it may perform is the full-text index row of a rejected synthesis, which
is a regenerable index entry over unchanged substrate, following the
shipped precedent in the synthesis worker's own supersession path.
Erasure remains a separate, future, explicitly-named tool for content
that must be gone; this route must never grow into it.

## Consequences

- The Mirror's last dead end closes: a wrong fact is actionable where the
  operator sees it, whether it lives in a source event or in a derived
  synthesis, from the same dashboard session.
- Correction is eventually consistent with derivation: a corrected source
  is marked not-current on the next Mirror fetch, but the affected
  syntheses re-derive on the worker's cycle, so up to six hours can pass
  before the derived layer reflects the correction. The UI states the
  window instead of pretending immediacy.
- The source of truth stays immutable. Corrected events, rejected
  syntheses, and suppressed key points all remain in the vault
  byte-identical, visible to history reads, and exportable.
- Suppression is projection-level. A synthesis payload is never
  rewritten; suppression markers attach at serving time from the
  interpretation overlay. Any future consumer of synthesis payloads must
  read the overlay too, or it will serve suppressed claims unmarked.
- Key-point identity is digest-based, so suppression carry-forward only
  survives a verbatim repeat of the same point text on re-synthesis. A
  reworded repeat of the same wrong claim misses the digest and serves
  unmarked. This is a documented limit of the MVP; b3 is the durable
  answer, because it stops the claim being restated at all.
- A new operator-initiated write surface exists on `/internal`. Its
  posture is containment by construction rather than a new trust
  mechanism: the existing short-lived subject-pinned dashboard JWT (I8),
  the existing rate limiter, hard input bounds, full observe audit, and
  reversibility of every write. No new credential class is introduced.
- Two maintenance rules to keep: the route never runs SQL (the substrate
  function is the only writer, the ADR-0008 rule applied again), and no
  UI copy or doc may describe any of this as deletion.

## Alternatives considered

**Mutate the wrong content in place.** Edit the event payload or rewrite
the synthesis's key points. Rejected without needing a second reason: I2.
The substrate is append-only and trigger-enforced; a correction that
rewrites history also destroys the record of what the vault believed
before, which is exactly what supersession exists to preserve.

**Route it through erasure.** Erasure already removes content from
serving. Rejected: erasure is a different tool for a different obligation,
content that must be GONE, with a tombstone and crypto-shred. A wrong
fact is not a must-be-gone fact; conflating the two would either weaken
erasure into a casual button or destroy history for corrections that only
needed a view change. The boundary is drawn in ADR-0001 and holds here.

**Blind re-synthesis as the automatic fix.** Invalidate the synthesis and
present the fresh one as the correction. Rejected as dishonest: the
worker re-derives from the same unchanged sources with the same model,
and nothing in that run steers away from the prior wrong claim, so the
fresh synthesis can reproduce the error. Re-synthesis is a consequence
the operator is warned about, not a remedy the product promises.

**Route it through `decide_correction` as a synthetic proposal.** Mint a
proposal at button-press and confirm it immediately, to reuse the single
mutation point. Rejected: it inverts the ADR-0002 semantics (that loop
exists for machine proposals awaiting human verdicts, and an operator
assertion is not one), pollutes the pending views and counts that ADR-0008
just made meaningful, and reuses machinery this path does not need. The
single-mutation-point rule defends operator DECISIONS on machine
proposals; operator-initiated writes are the same actor exercising the
same authority through a write path built for assertion.

## Invariant fit

- **I1**: no MCP tool is added, removed, or re-typed; the route is
  dashboard-only. The golden wire surface stays byte-identical, and the
  Mirror's serving shape gains additive fields only (suppression markers,
  currency flags).
- **I2**: every correction is a new record: an event, an invalidation, an
  interpretation, an observe. Nothing is mutated. The only deletion on
  any path is the regenerable full-text index row of a rejected
  synthesis, which is an index entry, not substrate. The
  `key_point_review` interpretation rows are protected by the
  append-only interpretation triggers hardened in ADR-0008's follow-up,
  so a suppression record can never be deleted out from under its marker.
- **I3**: no migration. Flavor A is shipped read semantics applied to a
  new writer. Flavor B's suppression layer is a new `produced_by`
  namespace, which is precisely the I3 mechanism: a new view over
  unchanged substrate, readable and re-interpretable alongside everything
  that came before.
- **I4 / I8**: operator-only, over the credential-gated single-tenant
  paths; the durable records (events, invalidations, interpretations,
  observes) all ride the vault export, so a self-hosted or exported vault
  carries its corrections with it.
- **I5**: every correction path is deterministic and makes no model call;
  b3, which would change the synthesis prompt, is deferred pending its own
  review and stays provider-neutral when it lands.
- **I7**: every correction is recorded (the observe trail plus
  `corrected_by` provenance) and reversible (re-validate the invalidated
  event, restore the suppressed point).
