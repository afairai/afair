# ADR-0004 - A calibrated confidence model for entity edges

> **Status:** Accepted
> **Date:** 2026-07-02
> **Audience:** anyone touching the entity graph, the cold-path agents, recall, or the tuner
> **Relates to:** [ADR-0002](ADR-0002-belief-revision-derived-layer.md) (belief revision, quarantine, edge_reviews), [ADR-0003](ADR-0003-emergent-ontology.md) (append-only overlays, read-time resolution), VISION.md §4 (I2/I3/I7)

> **Resolved fork (operator decision):** low-confidence `proposed` edges ARE
> served in recall WITH a caveat, not suppressed. Recall honesty means
> surfacing a tentative belief and flagging it, and it is what feeds the
> correction-on-recall loop. Implemented that way in the recall slice; the
> `RecallCoverage.low_confidence_edges` count + caveat line make the tentativeness
> explicit, and each such edge is reviewable through `recall(decide=...)`.

## Context

Every one of the 176 `entity_edges` rows in the live vault carries
`confidence = 0.8`, hardcoded at the single call site that writes edges
(`agents/entity_canonicalizer.py`, `write_entity_edge(confidence=0.8)`).
ADR-0002 already named "flat 0.8 confidence" as half of the original
confabulation problem. The evidence requirement fixed fabrication; the number
itself remained a constant that means nothing.

The consumer audit (done before this design) found:

1. **One consumer is already wired and vacuous.** `belief.auto_confirm`
   gates the quarantine queue on `confidence >= 0.75`
   (`_MIN_AUTO_CONFIRM_CONFIDENCE`). With every edge at 0.8, the floor never
   fires: the entire quarantine decision currently rests on predicate crispness
   alone. Recall (`mcp/handlers.py:641`) passes `edge.confidence` into this
   check on every hit.
2. **Recall serves `trust` per edge but not the number.** The AI client sees
   `proposed` vs `auto_confirmed` but cannot distinguish a 0.9 belief from a
   0.4 one.
3. **`edge_reviews` exists but nothing feeds it.** `record_edge_review` is
   implemented, tested, and exported, and has zero production callers. The
   `recall(decide=)` loop covers only entity-audit and ontology proposals
   (`proposed_corrections` / `ont_` prefix). The labeled set that ADR-0002
   promised the tuner ("the ground-truth set the self-improvement tuner
   lacks") cannot grow today.
4. **The article synthesizer launders weak beliefs.**
   `entity_articles._gather_edges` feeds every live edge into article prose
   regardless of trust or confidence.
5. **Temporal decay is not a consumer yet.** The temporal worker only gathers
   metadata ("P1 only GATHERS... recall behaviour is unchanged"). Wiring
   confidence into a ranking that does not exist would be speculative.

So the honest scope is: calibrate the number AND wire exactly these consumers:
the auto-confirm gate (fix the vacuous floor), recall edge views + the
coverage caveat (recall honesty), the edge-review proposal queue (which makes
calibration data exist at all), the article synthesizer filter, and the tuner
whitelist. Deliberately left out: temporal decay weighting (no decay ranking
exists yet), recall hit reordering (hits are events, edges are attachments to
them), and deep fusion of conflict-resolver pair verdicts (the event-pair to
edge mapping is indirect; a boolean "source event is contested" flag captures
the actionable part).

## Decision

Introduce a two-layer edge-confidence model:

1. **A write-time prior**, computed by a pure, explainable function from
   signals available when the canonicalizer writes the edge, stored in the
   existing `entity_edges.confidence` column on NEW rows only (the column
   becomes "confidence at discovery", an immutable snapshot).
2. **An append-only scoring overlay** (`edge_confidence_scores`), where a
   cold-path scorer appends re-computed scores as post-write signals
   accumulate (corroboration, contest) and where the 176 legacy rows receive
   their first real score without any migration. The **served confidence** of
   an edge is its latest score row, falling back to the stored column.

This is the same supersession pattern as `entity_kind_assignments` (ADR-0003)
and `edge_reviews` (ADR-0002): the base row is immutable, the current view is
the latest overlay row, reads compose at query time.

### The signal set

Each signal, with its provenance in the code:

| Signal | Type | Provenance | At write time | At rescore time |
|---|---|---|---|---|
| `extraction_confidence` | float or None | the extractor's whole-extraction self-assessment (`extraction["confidence"]`, optional field of `EXTRACTOR_TOOL_SCHEMA`) | in hand (the canonicalizer holds the extraction dict) | recovered from the source event's `extractor:%` interpretation row |
| `subject_mention_confidence`, `object_mention_confidence` | float or None | the mention confidence the canonicalizer just wrote for each endpoint in THIS event (exact=1.0, alias=0.9, llm=verdict.confidence, new=0.5) | in hand (tracked alongside the `resolved` dict) | recovered from `entity_mentions` rows for (event_hash, surface_form) |
| `predicate_crisp` | bool | `belief.predicate_is_crisp(predicate)`, the ADR-0002 confabulation tell | in hand | in hand |
| `corroborating_sources` | int >= 0 | count of OTHER live (non-invalidated) `entity_edges` rows asserting the same canonical triple (merge-resolved subject id, lowercased predicate, merge-resolved object id) from distinct source events. The UNIQUE constraint is on (subj, pred, obj, source_event_id), so independent re-assertion creates sibling rows: those siblings ARE the corroboration signal | SQL count | SQL count |
| `source_conflicted` | bool | the edge's source event carries an unresolved conflict verdict (`verdicts.is_unresolved_conflict` over `read_conflicts_batch`) | usually False (the resolver runs later) | real signal, this is the main reason rescoring exists |

The extractor's relations schema has NO per-relation confidence field, and we
do not add one: per-relation self-reported scores from the same model that
produced the relation add little beyond the extraction-level score, and the
tool schema churn is not worth it. The extraction-level score plus the
deterministic signals carry the information.

Not inputs, on purpose: recency (confidence answers "is this true", not "is
this current"; `valid_to`, invalidations, and the future decay layer own
currency), and the source entrenchment tier (recall currently hardcodes
`AGENT_DERIVED`; foreign-import downgrading is its own ADR-0002 slice and
already acts on `auto_confirm` independently of the number).

### The computation

A transparent log-odds (logit-space) sum. Pure function, no DB, no LLM, lives
next to `belief.py` in `afair/substrate/confidence.py`:

```
z = logit(BASE_RATE)                                        # 0.70 -> 0.847
  + W_EXTRACT * (extraction_confidence - 0.7)   if present  # else 0
  + (+W_CRISP if predicate_crisp else -W_CRISP)
  + W_MENTION * (min(available mention confs) - 1.0)        # <= 0; else 0 if none
  + W_CORROBORATION * log2(1 + corroborating_sources)
  - (W_CONFLICT if source_conflicted else 0)

confidence = clamp(sigmoid(z), 0.05, 0.99)
```

Defaults: `BASE_RATE = 0.70`, `W_EXTRACT = 1.5`, `W_CRISP = 0.4`,
`W_MENTION = 2.0`, `W_CORROBORATION = 0.8`, `W_CONFLICT = 1.0`.

Properties, by construction:

- **Explainable.** Every score is a sum of named terms; the full breakdown
  (signal values, weights, per-term contribution, z) is stored as JSON next to
  the score. "Why 0.63?" has a stored answer.
- **Graceful degradation.** Every missing signal contributes exactly 0 (the
  terms are deviations from a neutral point). A legacy edge whose extraction
  row is unrecoverable still gets a sensible score from crispness +
  corroboration alone.
- **Weakest-link endpoints.** The `min()` over mention confidences mirrors
  the AGM rule ADR-0002 already adopted: a belief is never more entrenched
  than its least-entrenched justification. An edge anchored on a `new`
  (0.5) endpoint is strongly discounted (`2.0 * -0.5 = -1.0` in z).
- **Sane anchor points.** A typical well-grounded edge (extraction 0.9, crisp,
  exact endpoints) lands at ~0.82, close to the historical 0.8, so trust-state
  behavior does not swing wildly on day one. A weak edge (no extraction score,
  vague 5-word predicate, one new endpoint) lands at ~0.37 and is quarantined.
  Two independent corroborating sources push a strong edge to ~0.94. A
  contested source drops a strong edge to ~0.63, below the auto-confirm floor:
  contested beliefs go back to `proposed`. All four behaviors are the ones the
  belief layer wants.
- **Never certainty.** The clamp at 0.99 (and 0.05) keeps an agent-derived
  belief from being served as fact, in line with the recall honesty layer.

### Calibration

There is no labeled ground truth yet, and this ADR does not pretend otherwise.
The bootstrap is explicit:

1. **Early scores are heuristic priors.** The weights above are hand-set
   anchors, documented as such. What makes them non-arbitrary is the stored
   per-term breakdown and the measurement loop below.
2. **The labeled set is the operator's own verdicts.** `edge_reviews`
   (confirm/reject) is exactly the calibration target: an edge served at 0.9
   should be confirmed ~90% of the time. Today that table is empty because
   nothing surfaces edges for review; this design fixes that (consumer C4
   below): the scorer proposes the lowest-confidence unreviewed `proposed`
   edges into the existing `proposed_corrections` queue (kind
   `edge_review`), and `recall(decide=)` verdicts land in `edge_reviews`
   through the already-shipped `record_edge_review` (a reject also writes the
   `edge_invalidation`, as built). Bounded proposals per cycle: quarantine
   research says queue only the uncertain, so review effort stays small.
3. **Measurement before adjustment.** A `calibration_report(conn)` helper
   computes, over all reviewed edges: per-bucket observed confirm-rate vs
   mean predicted confidence, Brier score, and counts. The scorer logs it in
   its cycle stats. Below `CALIBRATION_MIN_REVIEWS = 20` labeled edges (with
   at least 5 in each class) the report says "insufficient data" and nothing
   moves.
4. **Adjustment is a tunable, inside I7's fences.** `BASE_RATE` and
   `W_CORROBORATION` enter the tuner whitelist (`tunable_registry.REGISTRY`,
   worker `edge_confidence`) with hard bounds and bounded per-promote delta,
   like every other tunable. The auto-confirm floor moves too (worker
   `belief`, `auto_confirm_floor`, default 0.75, bounds [0.60, 0.90]). The
   calibration report is the evidence payload for a promote. The tuner remains
   `promote_enabled=False` by default; this design produces the ground-truth
   signal ADR-0002 said it was waiting for, it does not flip the switch.
5. **Re-derivation is a version bump.** Scores are stamped
   `computed_by = 'edge_confidence:v1'`. A model change bumps the version and
   the scorer re-scores everything, same pattern as the temporal worker. Old
   score rows remain as history (I7: recorded, reversible).

### Storage

New append-only table, protected by the same I2 triggers as the other entity
tables:

```sql
CREATE TABLE IF NOT EXISTS edge_confidence_scores (
    id           TEXT PRIMARY KEY,            -- ULID
    edge_id      TEXT NOT NULL REFERENCES entity_edges(id),
    confidence   REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    components   TEXT NOT NULL,               -- JSON: signals, weights, terms, z
    computed_by  TEXT NOT NULL,               -- 'edge_confidence:v1'
    computed_at  TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS edge_confidence_scores_edge_idx
    ON edge_confidence_scores(edge_id, computed_at);
-- plus no-update / no-delete RAISE(ABORT) triggers (I2)
```

- **New edges**: the canonicalizer computes the prior, passes it to
  `write_entity_edge(confidence=prior)` (replacing the literal 0.8), and
  appends the initial score row right after. If the score write fails the
  edge still stands and the fallback (stored column) serves the identical
  number; the rescorer self-heals the missing row. No torn state can mislead.
- **Old edges (the 176 flat-0.8 rows)**: never touched (I2 forbids it and the
  DB triggers enforce it). The cold-path scorer appends their first score row,
  computed by the same pure function over signals recovered from the substrate
  (the source event's extractor interpretation for the extraction confidence
  and evidence; `entity_mentions` for endpoint confidences; live sibling
  edges for corroboration). Unrecoverable signals contribute their neutral 0.
  This is I3 exactly as written: a new view over unchanged substrate, no
  migration.
- **Served confidence** (one helper, used by every consumer):
  `latest_edge_confidence_batch(conn, edge_ids)` returns the newest score row
  per edge regardless of version; absent rows fall back to
  `entity_edges.confidence`. Old vaults and mid-backfill vaults keep working
  (they just see 0.8 until the scorer reaches them).
- **Idempotency without UNIQUE gymnastics**: the scorer appends only when no
  row exists at the current `computed_by`, or when the recomputed value
  differs from the latest by `>= 0.01`. Re-runs are no-ops; signal changes
  produce history.
- **Export**: the table joins the export stream after `entity_edges`
  (FK-safe). It is technically re-derivable, but the rows are small, the
  history is part of "what the system believed when" (I4 completeness), and
  including them costs nothing.

### Consumer wiring

| # | Consumer | How it uses confidence | Constants (all named, most tunable) |
|---|---|---|---|
| C1 | `belief.auto_confirm` via recall's trust resolution | gains a `floor` parameter; recall passes the SERVED confidence (latest score, not the frozen write-time column) and the tunable floor. The quarantine gate becomes discriminating for the first time | `belief.auto_confirm_floor` tunable, default 0.75, bounds [0.60, 0.90] |
| C2 | recall edge views | each served edge gains `"confidence": <served, rounded 3>` next to `"trust"`. Additive response field, I1-safe | round to 3 decimals |
| C3 | `RecallCoverage` honesty layer | new optional field `low_confidence_edges: int` plus a caveat line when any served edge in the results is below the threshold and unreviewed: "N relation(s) in these results are low-confidence beliefs; treat as tentative" | `LOW_CONFIDENCE_EDGE_CAVEAT_THRESHOLD = 0.5` |
| C4 | edge-review proposal queue | the scorer proposes the K lowest-confidence, unreviewed, non-invalidated `proposed` edges into `proposed_corrections` (kind `edge_review`); `decide_correction` dispatches confirm/reject to `record_edge_review`. This gives `record_edge_review` its first production caller and makes the calibration set grow | `EDGE_REVIEW_PROPOSAL_THRESHOLD = 0.6`, `MAX_EDGE_REVIEW_PROPOSALS_PER_CYCLE = 3` |
| C5 | `entity_articles._gather_edges` | skips edges whose served confidence is below the floor, so weak beliefs stop being laundered into confident article prose (rejected edges are already filtered by invalidation) | `ARTICLE_MIN_EDGE_CONFIDENCE = 0.4` |
| C6 | tuner | `edge_confidence.base_rate`, `edge_confidence.corroboration_weight`, `belief.auto_confirm_floor` join the whitelist; `calibration_report` is the evidence source | bounds and deltas in the registry rows |

Deliberately not wired (with reasons, so nobody re-derives this):

- **Temporal decay input**: the decay ranking itself does not exist yet
  (temporal P1 gathers only). Revisit when decay wires into recall ranking.
- **Recall hit reordering**: hits are events ranked by FTS/vector/entity
  match; edges are attachments. Down-ranking an event because one attached
  edge is weak would punish good memories for bad interpretations.
- **Conflict-verdict fusion beyond the boolean**: pair verdicts
  (`confirms`/`conflicts`) relate events, not triples. The deterministic
  sibling-edge count already captures corroboration directly; the boolean
  `source_conflicted` captures contest. Finer mapping is future work if the
  calibration report shows systematic miscalibration on contested edges.

## Consequences

- The quarantine gate starts doing its job: weakly-grounded edges (vague
  predicate, new endpoints, no extraction confidence, contested source) fall
  below the floor and queue for review; well-grounded ones skip it. Review
  effort stays bounded by design.
- Recall gets numerically honest about relations: clients can distinguish "the
  vault is fairly sure" from "the vault guessed", and the coverage layer says
  so in words.
- The operator's confirm/reject verdicts finally accumulate, which (a)
  calibrates this model and (b) unblocks the tuner's ground-truth gap noted in
  ADR-0002. One loop, two payoffs.
- Two sources of truth for one edge's confidence exist (stored column vs
  latest score). Mitigated by a single accessor everywhere
  (`latest_edge_confidence_batch` with fallback) and by documenting the column
  as "confidence at discovery". No consumer reads the raw column directly.
- The scorer adds one more cold-path worker (SQL + JSON only, no LLM, no
  budget pressure).
- Early numbers are only as good as the hand-set weights. Accepted: the
  breakdown is stored, the calibration report measures reality, and the
  intercept is tunable within I7's fences. The alternative (keep the
  constant) is strictly worse.

## Alternatives considered

- **Recompute purely at read time, no scoring table.** Rejected: the signal
  recovery for legacy edges (parse interpretation JSON, match relation rows to
  edges, count siblings) is too heavy for the recall hot path, and the score
  history (what did we believe last month, and why) would be lost. The
  overlay table is the house pattern.
- **Mutate `entity_edges.confidence` in place.** Rejected outright: I2, and
  the DB triggers physically refuse.
- **A learned model (logistic regression / small MLP) over the signals.**
  Rejected for v1: with 0 labeled examples there is nothing to fit, and an
  opaque score would violate the explainability requirement. The log-odds
  form IS a logistic model with hand-set coefficients; when `edge_reviews`
  grows, fitting those same coefficients is a natural, shape-preserving
  upgrade (a new `computed_by` version).
- **Per-relation LLM self-scores in the extractor schema.** Rejected: schema
  churn on the frozen-ish extractor contract, self-scores from the producing
  model are weakly informative, and the extraction-level score already
  carries most of it.
- **An LLM judge re-scoring each edge.** Rejected: cost scales with the
  graph, verdicts drift with the judge model, and it reintroduces opacity.
  Deterministic signals + operator verdicts are cheaper and auditable.
- **Suppress low-confidence edges from recall entirely.** Rejected: recall
  honesty means surfacing with a caveat, not hiding. Suppression also starves
  the reconsolidation loop (correction happens at recall time, ADR-0002 §5).

## Invariant compliance

- **I1 (MCP surface)**: no verb changes. Recall's response gains optional
  additive fields (`confidence` per edge view, `low_confidence_edges` in
  coverage); the review flow rides the existing `recall(decide=)` argument
  and the existing `proposed_corrections` queue, exactly like ontology
  proposals did in ADR-0003.
- **I2 (append-only substrate)**: no existing row is mutated. New edges get a
  computed value in an existing column at insert time; everything after
  insert is new rows in a new append-only table with its own no-update /
  no-delete triggers. The 176 legacy rows stay byte-identical forever.
- **I3 (backward-compatible evolution)**: no migration. Legacy edges remain
  readable and gain a served confidence through the same pure function over
  recovered signals; a vault that never runs the scorer still works (column
  fallback). Old exports import cleanly (the new table is additive).
- **I6 (emergent over imposed)**: no ontology is introduced. Predicate
  crispness is shape-based (word count), not an enum; predicates remain free
  text.
- **I7 (recorded, reversible self-modification)**: every score row records
  its full explanation and version; re-derivation is a version bump with
  history kept; the only self-tunable parameters are whitelisted in the
  registry with hard bounds and bounded deltas; a promote is recorded in
  `tuner_state` and reversible by rollback. The scorer itself and the
  formula's structure are NOT on the tunable surface.

## References

- ADR-0002 (this repo): entrenchment, quarantine, `edge_reviews`, the
  tuner's ground-truth gap.
- Triple Trustworthiness Measurement for KGs: https://arxiv.org/pdf/1809.09414
- LLM + human-in-the-loop KG validation (2025): https://www.sciencedirect.com/science/article/pii/S030645732500086X
- Platt scaling / calibration of probabilistic classifiers (background for
  the intercept-recalibration upgrade path).
