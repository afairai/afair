# ADR-0005 - Telemetry tables are prunable operational data, not memory

> **Status:** Accepted
> **Date:** 2026-07-06
> **Audience:** anyone touching the substrate schema, the cold-path Pruner, `/health`, `timeline()`, or reasoning about Invariant I2
> **Relates to:** VISION.md §4 (I2 append-only, I3 re-interpretable, I4 user owns the substrate), [ADR-0002](ADR-0002-belief-revision-derived-layer.md) / [ADR-0003](ADR-0003-emergent-ontology.md) (the `proposed_corrections` / `proposed_ontology_revisions` non-substrate exemptions), the Phase 0.5 observability work

## Context

Two tables grow without bound and can never be pruned today because they carry
the same append-only I2 triggers as the memory substrate:

- **`pipeline_events`** — one row per lifecycle stage of every event
  (`event.written`, `extraction.enqueued`, `…completed`/`…failed`,
  `embedding.stored`, `canonicalizer.processed`, …) **plus** a per-cycle marker
  from every cold-path worker. 22 files write via `pipeline_events.record`.
  Growth drivers at steady state: ~4–6 lifecycle rows per user event, plus
  per-cycle worker markers — canonicalizer/120s (~263k/yr), temporal/180s
  (~175k/yr), edge_scorer/240s (~131k/yr), salience, mode_switcher. A 2–3 year
  vault reaches ~1M rows.
- **`observability_snapshots`** — one integer-only counter row per
  expectation-checker cycle (15 min → ~35k rows/year, as the schema comment
  already notes).

The Pruner (`agents/pruner.py`) already ages out OAuth codes, stale-failed
interpretations, and decided edge-review queue rows — but it **cannot** touch
these two: their `RAISE(ABORT, '… is append-only (Invariant I2)')` triggers
abort any DELETE. So the flight recorder grows forever, and its own scans (the
expectation-checker's window queries, `timeline()`) get slower as it does.

The question this ADR answers is constitutional: **is the flight recorder part
of the substrate that I2 protects?**

## Decision

**Classify `pipeline_events` and `observability_snapshots` as OPERATIONAL
TELEMETRY, not user memory. Retire their append-only triggers and let the
Pruner age rows out past a retention window.**

The honest reading of I2/I4: I2 says *"the substrate is append-only,
content-addressed"* and I4 says *"the user owns the substrate."* What the user
**remembers** — the events, interpretations, entities, edges, temporal/belief
metadata — is the substrate those invariants protect. `pipeline_events` and
`observability_snapshots` are instrumentation about *how the plumbing ran*.
They carry no user memory, are never recalled, and are content-addressed by
nothing. They are already conceptual siblings of `proposed_corrections`
("MUTABLE derived state, not substrate") and `export_jobs` ("MUTABLE
operational table") — both of which the codebase **deliberately exempted** from
the append-only triggers for exactly this reason.

Under that reading, pruning the flight recorder is no more an I2 erasure than
rotating a log file is. Concretely:

1. **Schema.** Append idempotent `DROP TRIGGER IF EXISTS` for the four triggers
   (`pipeline_events_no_update`/`_no_delete`,
   `observability_snapshots_no_update`/`_no_delete`) — this retires them on
   existing vaults. The `CREATE TABLE` DDL no longer recreates them, so a fresh
   vault never has them. The table comments are rewritten to declare both
   tables operational telemetry (mirroring `proposed_corrections`).
2. **Pruner.** A new `telemetry_retention_days` setting (default **90**) and a
   `_prune_telemetry` step in the existing 6-hour loop delete
   `recorded_at < cutoff` from both tables, batched (5000 rows/DELETE) so a
   first prune on a multi-year vault never holds one giant write lock. It emits
   a `pruner.telemetry_pruned` count.
3. **Constitution.** VISION §5 and CLAUDE.md §5 previously implied I2 triggers
   on *all* derived tables; both are corrected to name the non-substrate
   telemetry/operational exceptions explicitly, so code and constitution agree.

Only these two tables lose their triggers. Every user-memory and derived-belief
table (`events`, `interpretations`'s substrate lineage, `entities`,
`entity_mentions`, `entity_edges`, `entity_merges`, `edge_invalidations`,
`edge_reviews`, `edge_confidence_scores`, `entity_retractions`,
`entity_kind_assignments`, `entity_identities`, `kind_*`, `event_temporal`,
`tuner_state`, …) keeps its append-only triggers unchanged.

## Consequences

- **Positive.** Bounded flight-recorder growth; the expectation-checker's own
  scans and `timeline()` stay fast at vault-age; the Pruner finally owns *all*
  interpretation-layer/operational maintenance.
- **The cost — a one-way relaxation.** This relaxes triggers the code
  previously advertised as I2. A future reader must not confuse "telemetry we
  chose to make prunable" with "memory we must never delete." This ADR draws
  that line explicitly and durably; the schema comments and VISION/CLAUDE §5
  edits carry it inline where the next person will read it. Reversal is a
  one-line re-add of the four triggers.
- **`timeline()` over the retained window stays correct.** Pruned telemetry is
  instrumentation, not re-interpretable memory — nothing recalls it, so I3 is
  not engaged (there is no substrate view to keep readable). The default
  90-day window is generous for live diagnosis; older gaps were already a
  one-time backfill concern, not live monitoring.
- **I4 unchanged.** The user still owns 100% of their *memory* substrate;
  export continues to carry all memory tables. Telemetry retention is disclosed
  here and in the setting's description.

## Alternatives considered

**Option B — archive-to-blob + a `_v2` view, keep the triggers.** Roll old rows
into a compressed content-addressed blob and serve reads through a UNION view.
But the triggers forbid DELETE, so this **cannot actually shrink the live
table** — "archive **and** delete" is impossible without also dropping the
triggers, which collapses B into A. The only trigger-free variant is to *stop
writing* the high-volume per-cycle markers to `pipeline_events` (route them to a
separate non-triggered table or to logs only) and keep `pipeline_events` for the
low-volume per-user-event lifecycle rows — i.e. split the table, keep two code
paths, and add a UNION view for `timeline()`/the checker to read across both.
That is more surface and more places for `timeline()` to go blind, for a benefit
(literal trigger preservation) that is only meaningful if one accepts the flight
recorder **is** protected memory — the exact premise this ADR rejects.

**Rejected:** Option A is simpler, honest, and consistent with two existing
exemptions the codebase already made for operational tables.

## Invariant fit

- **I1** untouched (no MCP surface change).
- **I2** — the entire subject of this ADR: the line between *memory* (protected,
  append-only) and *telemetry* (prunable operational data) is drawn here and
  cross-referenced from VISION §4/§5.
- **I3** — n/a: pruned telemetry is not re-interpretable memory, there is no
  substrate view to keep readable.
- **I4** — the user still owns and can export all memory; telemetry retention is
  disclosed.
- **I7** — this ADR is itself a recorded, reversible constitutional
  interpretation: re-adding the four triggers reverses it.
