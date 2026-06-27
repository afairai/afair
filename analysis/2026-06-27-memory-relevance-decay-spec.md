# Memory relevance and temporal decay — design spec

> **Document version:** 1.0
> **Status:** Implemented (P1–P4 shipped 2026-06-27)
> **Audience:** afair maintainers
> **Shipped:** P1 extract (6655a49), P2 decay-in-ranking (277cdcc), P3 recurrence
> re-surfacing + session-start `upcoming` (859f4da), P4 topic warmth (dffdbed).
> Lives in `afair/substrate/temporal.py`, `afair/agents/temporal.py`, the recall
> re-rank in `afair/mcp/handlers.py`, and the `upcoming` field in
> `afair/mcp/resources.py`. Half-lives + windows are module constants, ready to
> hand to the self-improvement tuner.
> **Origin:** raised 2026-06-27 (Gowry). Appointments and deadlines matter until
> their date, then fade; birthdays and anniversaries recur. And the same shape
> applies well beyond dates: a finished project, a superseded fact, a passing
> question. Recall should surface what is relevant *now* and let the rest settle,
> without ever deleting it.

## 0. Current state

afair stores everything append-only (I2) and keeps it forever-readable (I3).
Recall fuses FTS5, vector similarity, the entity layer, and an article-first
ordering; a salience worker scores events, a surprise score flags novelty, and a
mode-switcher routes attention. What is missing is a **time dimension on
relevance**: a recall for "current state" treats a dinner from last March, a
deadline that already passed, and your sister's birthday next week the same way
they were the day they were written. The substrate never forgets, which is
correct, but recall has no notion that some memories have *settled* and others
are *coming due*.

This spec defines how relevance changes over time, the cases it covers, and how
to implement decay and re-surfacing **without deleting anything** and without a
fixed ontology.

## 1. The problem

Relevance is not static. A memory's usefulness to a *current-state* question
("what's on my plate", "what does Mara like") changes as time passes and as the
world moves. Two failure modes today:

- **Stale surfacing.** A one-off event stays as retrievable a year later as it
  was the day it happened. Past appointments, closed deadlines, and finished
  trips compete with live context.
- **Missed re-surfacing.** A birthday or an annual filing is written once and
  then sits flat; nothing lifts it back up as its next occurrence approaches.

The constraint: afair is append-only (I2) and forever-readable (I3). So
"forgetting" here is never deletion. It is a **relevance weight applied at
recall time**, plus salience decay, both reversible and recorded (I7). History
stays fully queryable; only the *default, current-state* ranking changes.

## 2. Taxonomy of relevance over time

The owner's point: this is not only about dated appointments. Eight cases, each
with a distinct temporal behavior. The class is **inferred from content cues by
the extractor**, never a hardcoded enum the user must fit into (I6).

| # | Class | Example | Behavior |
|---|---|---|---|
| 1 | **Dated one-off** | "dinner with Mara Saturday", "deadline Friday", "flight on the 12th" | Relevant up to the event time; decays sharply after it passes. Stays in history. |
| 2 | **Recurring** | birthday, anniversary, weekly standup, monthly rent | Re-surfaces near each occurrence; otherwise quiet between cycles. |
| 3 | **Superseded fact** | "I work at elvah" → "I work at Athara"; moved city; renamed project | Old fades the moment it is invalidated; current value surfaces. History on demand. Builds on the existing `invalidates` mechanism. |
| 4 | **Decaying topic** | an active project that ships; a trip you were planning, now taken | No hard date; relevance fades as the topic goes quiet (no fresh related events). Re-warms if the topic returns. |
| 5 | **Transient / ephemeral** | a one-off lookup, a passing mood, "what's 2+2" | Low durable value; should fade fast, or never be written. |
| 6 | **Evergreen** | your name, core values, a stable preference, who your kids are | No decay. Timeless. |
| 7 | **Periodic / seasonal** | tax season, the annual review, "renew the passport every 10 years" | Re-surfaces on a season/long cycle. A slow-period special case of (2). |
| 8 | **Commitment until done** | "I promised to send Jonas the contract" | Salient until fulfilled, then settles. Date-soft; closure-driven, not clock-driven. |

Note that (3) and (8) are **state-driven** (an invalidation, a fulfillment),
(1)(2)(7) are **clock-driven** (an event time / recurrence), and (4)(5) are
**activity-driven** (topic warmth). The design must handle all three drivers.

## 3. Design principles (within the invariants)

- **Never delete (I2).** Decay is a *score*, not a `DELETE`. Every temporal
  signal is a new append-only derived row, never an edit of the event.
- **History stays readable (I3).** Decay applies to the *default* current-state
  recall. A history/as-of recall sees everything flat, undecayed.
- **No imposed ontology (I6).** Temporal class, event time, and recurrence are
  *inferred* by the extractor from the content, with confidence. The user never
  picks a type. Low-confidence inferences decay gently or not at all.
- **Recorded and reversible (I7).** The decay function and its parameters are
  versioned and tunable; a re-derivation can rebuild the temporal layer from the
  unchanged substrate. The self-improvement tuner can later tune the decay curve
  the same way it tunes salience.
- **Frozen surface (I1).** No new MCP tool. This rides on `recall` ranking and
  the existing `afair://session-start` resource (additive fields only); an
  optional `recall` arg (e.g. `as_of` / `temporal="history"`) is additive and
  allowed.

## 4. Mechanism

### 4.1 Temporal metadata (derived, append-only)

The extractor (cold path) adds, per event, an optional temporal record in the
interpretation layer. It does **not** touch the event row.

```
event_temporal (
  event_id            TEXT,        -- the event this describes
  content_hash        TEXT,        -- content addressing
  temporal_class      TEXT,        -- one-off | recurring | superseded
                                   --  | decaying | transient | evergreen
                                   --  | periodic | commitment  (inferred)
  event_time          TEXT NULL,   -- when the thing happens (ISO 8601), if any
  relevance_horizon   TEXT NULL,   -- when current-state relevance should fall off
  recurrence_rule     TEXT NULL,   -- RFC 5545 RRULE-ish, for classes 2 and 7
  closure_state       TEXT NULL,   -- open | fulfilled | superseded (classes 3, 8)
  confidence          REAL,        -- extractor confidence in the above
  computed_by         TEXT,        -- model + prompt version (re-derivable)
  created_at          TEXT
)  -- append-only; I2 triggers forbid UPDATE/DELETE
```

Re-derivable from the unchanged substrate at any time (a backfill worker), like
the entity graph. Timezone is captured on `event_time` or defaulted to the
vault's configured zone; ambiguous dates get low confidence.

### 4.2 Temporal relevance signal at recall

A pure function `temporal_relevance(record, now) → [0,1]` feeds the recall
ranking as one more factor alongside FTS, vector, entity-article, and salience:

- **One-off (1):** ~1 up to `event_time`, then a decay (e.g. half-life of days
  to weeks past `relevance_horizon`). Never reaches 0 (history still findable),
  floors at a small ε.
- **Recurring / periodic (2, 7):** a bump as `now` approaches the next
  occurrence from `recurrence_rule`; low between cycles.
- **Superseded (3):** the invalidated event floors to ε for current-state; the
  superseding event carries full weight. (Today's `invalidation` already marks
  this; this formalizes its ranking effect.)
- **Decaying topic (4):** weight tied to topic warmth = recency of the most
  recent related event (same entity/article). Re-warms on new activity.
- **Transient (5):** fast decay, short half-life.
- **Evergreen (6):** constant 1. No decay.
- **Commitment (8):** ~1 while `closure_state = open`; drops on `fulfilled`.

The factor **multiplies** into the fused score for default recall. It is a
ranking nudge, not a filter: nothing is excluded, only re-ordered.

### 4.3 Salience decay

The salience worker gains a recency/activity decay term so a quiet topic's
salience drifts down over time and re-rises when the topic returns. This makes
class (4) emergent rather than explicitly dated. Decay is monotone-reversible:
re-running on the unchanged substrate reproduces the same curve.

### 4.4 Recall modes

- **Default (current-state):** temporal relevance applied. "What's relevant now."
- **History / as-of:** temporal relevance **off** (flat). Reached via the
  existing `depth="deep"` or a new additive `recall(temporal="history")` /
  `recall(as_of=<ISO>)` arg. "What did I know about X back then", "show me past
  appointments". This preserves I3 in spirit and in the API.

### 4.5 Session-start: what's coming due

`afair://session-start` gains an additive `upcoming` field: recurring and dated
items whose next occurrence is near (her birthday in 5 days, the filing next
week, the promised contract still open). Sits alongside `recent_salient_events`,
`open_threads`, and `pending_corrections`. This is the re-surfacing half of the
feature: afair proactively lifts a memory back up as it comes due.

## 5. Schema impact

Additive only. One new derived table (`event_temporal`), append-only with the
standard I2 triggers. No change to `events`. No destructive migration (I3).
Backfillable by a one-shot worker, like `scripts/backfill_entities.py`.

## 6. Cold-path worker

A `temporal` worker (or an extension of the extractor + salience) that, per new
event: infers the temporal class + times + recurrence, writes `event_temporal`,
and lets the salience decay term do the activity-driven part. Idempotent, bounded
per cycle, re-derivable. Registered in the cold-path scheduler next to the
entity canonicalizer and consolidator.

## 7. Phasing

- **P1 — extract.** Infer and store `event_temporal` (class, event_time,
  horizon, recurrence, closure). No behavior change yet; observe accuracy.
- **P2 — decay.** Wire `temporal_relevance` into default recall ranking for the
  clock-driven and superseded classes (1, 3, 7). Keep history mode flat.
- **P3 — re-surface.** Recurrence bumps + the `upcoming` session-start field
  (classes 2, 7, 8).
- **P4 — topic warmth.** Salience decay term for the activity-driven class (4),
  and transient fast-decay (5). Hand the decay curve to the tuner.

Each phase ships behind the same frozen verbs, validated against real recall
metrics before it changes ranking (same gate as the self-improvement loop).

## 8. Risks and open questions

- **False decay.** Burying something still relevant is worse than keeping noise.
  Floor at ε, never exclude; bias toward *under*-decaying on low confidence; let
  the user pin "keep surfacing this" (a remember with an evergreen cue).
- **Date/timezone ambiguity.** "next Friday", relative dates, missing year.
  Resolve against the vault timezone + write time; low confidence when unsure.
- **Recurrence parsing.** Keep to common RRULE shapes first; everything else is a
  one-off until a second occurrence is observed (let recurrence *emerge*).
- **Interaction with surprise/salience.** Temporal relevance and surprise can
  disagree (a surprising-but-expired event). Define the precedence; likely
  surprise informs *write*, temporal informs *current-state read*.
- **No silent loss.** Because nothing is deleted and history mode is flat, a
  mis-decay is always recoverable. Log what decayed for auditability.

## 9. Invariant check

- **I1** — no new tool; rides on `recall` ranking + additive `session-start`
  fields + at most an additive optional `recall` arg. Shipped signatures
  unchanged.
- **I2** — `event_temporal` is append-only; decay is a score, never a delete.
- **I3** — events unchanged; history/as-of recall stays flat and complete; the
  temporal layer is re-derivable.
- **I6** — temporal class inferred from content, not an enum the user fills in.
- **I7** — decay function versioned, tunable, reversible; re-derivation rebuilds
  it from the unchanged substrate.

## 10. Out of scope (for now)

Calendar write-back, notifications/push, and any external-source ingestion of
dates (Gmail/Calendar) stay out: those belong to the trust-ladder connectors,
not the substrate. This spec is purely about how the vault's own memories decay
and re-surface in recall.

## 11. Post-launch roadmap (planned, not yet built)

Shipped P1–P4 are honest but not the ceiling. Three follow-ups are deliberately
deferred (so the open-source release ships a complete, working feature rather
than half-built depth), each with its trigger:

- **Decaying → real topic activity (gap 2b).** Today the `decaying`/`transient`
  classes decay against the memory's own age (`created_at`), a proxy. The real
  signal is topic *activity*: the recency of the most recent event sharing an
  entity (via `entity_mentions`). A still-active old topic should stay warm; a
  quiet one should fade. Trigger: when the age proxy demonstrably mis-ranks on
  the benchmark. Note: gap 2a (superseded → the authoritative invalidation
  signal) is **done** (recall floors actually-invalidated memories).
- **Tuner-owned decay curves.** The half-lives, windows, and floors are module
  constants ready to hand to the self-improvement tuner (register `worker=
  "temporal"` specs; read effective values via `TunableRegistry` at the recall
  call-site; add a temporal replay/judge shape). Blocked on the same thing the
  tuner is globally blocked on: a ground-truth eval-set as the promote metric.
  The retrieval-quality `stale-demotion` family is the start of that set.
- **Local / distilled classifier.** The temporal worker calls an LLM per event.
  A small local model (distilled from the LLM as teacher, the labels it already
  produces plus user corrections) would remove per-event egress, a strong fit
  for afair's sovereignty stance. I5 makes the swap a config change
  (`ollama/...`); the benchmark is the measuring stick. The 8-way classification
  distills cleanly; date/recurrence extraction is the harder part and likely
  stays LLM or rule-based (a hybrid). Trigger: cost/privacy/scale.
