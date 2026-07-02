# ADR-0003: Emergent ontology (revisable entity kinds + Schema-Evolver)

> **Status:** Accepted (2026-07-02)
> **Date:** 2026-07-01
> **Audience:** anyone touching the entity graph, the cold-path agents, or recall
> **Relates to:** [ADR-0001](ADR-0001-constitutional-invariants.md) (invariants), [ADR-0002](ADR-0002-belief-revision-derived-layer.md) (quarantine + operator-confirm precedent), VISION.md §4 (I2/I3/I6/I7), §5.5 (emergent ontology), §6.5 (Schema-Evolver)

## Context

Invariant I6 says: no fixed ontology ships with the system; a minimal bootstrap
scaffold is acceptable, and the system must be able to revise, merge, split,
and discard categories based on usage, forever. Event-level typing already
honors this (`best_guess_kind` is free text, the extractor prompt explicitly
says "do NOT constrain to a fixed enum"). The flagship emergent entity graph
does not. VISION.md §4 records the debt in the I6 text itself: "entity kinds
still use a fixed seven-value bootstrap enum that the system does not yet
revise."

The debt is concrete and lives in four places:

1. **Three hardcoded copies of the same enum.** The extractor tool schema
   (`agents/prompts.py`, `entities.items.type` enum), the canonicalizer
   (`agents/entity_canonicalizer.py`, `_VALID_KINDS` + `_normalize_kind`),
   and the correction validator (`substrate/corrections.py`, `ENTITY_KINDS`)
   each hardcode `person / organization / place / project / product /
   concept / other`. Adding, renaming, or removing a kind is a three-file
   code change, which is the definition of an imposed ontology.

2. **Kind is baked into entity identity.** `substrate/entities.py`
   derives `entity_id = "entity:" + sha256(lower(canonical_name) + "|" + kind)`.
   An entity's kind therefore cannot change without changing its identity.
   Today's `retype_entity` works around this by creating a *new* entity under
   the target kind and merging the old one into it, growing a merge chain per
   correction. A registry-level revision (merge kind `organization` into a new
   kind `company`) would require O(entities-of-that-kind) new entities plus
   merge rows: the identity scheme structurally punishes exactly the
   operations I6 demands.

3. **The identity coupling causes real, observed damage.** The
   entity-deduplicator exists almost entirely because the LLM labels the same
   real-world thing `product` in one event and `project` in another, splitting
   it into two identities that Stage-1 exact match can never unify (a vault
   audit found 15 such clusters). The `merge_review` proposal type and the
   cycle-avoidance dance in `_retype_merged_entity` (invalidate the original
   merge before re-typing, or `project → product → project` closes a loop) are
   further complexity that exists only because kind is identity-bearing.

4. **The Schema-Evolver named in VISION §6.5 does not exist.** "Schema-Evolver
   revises ontology (planned; not yet implemented, the entity dedup and audit
   workers are its v0)." No component reads usage and proposes ontology
   revisions. I6's "based on usage, forever" has no executor.

These are one problem. As long as the kind set is code and the kind is
identity, no agent *can* revise the ontology, so no Schema-Evolver can exist.
This ADR designs the fix end to end: kinds become data, kind decouples from
identity, and a Schema-Evolver cold-path worker proposes revisions that the
operator confirms through the ADR-0002 quarantine loop.

## Decision

Three coupled moves, each append-only over the unchanged substrate.

### 1. Kinds become data: an append-only kind registry

Two new append-only tables (standard I2 trigger pair on both, appended to
`SCHEMA_DDL` per the additive-only rule):

```sql
CREATE TABLE IF NOT EXISTS kind_registry (
    id              TEXT PRIMARY KEY,      -- 'kind:<slug>'
    slug            TEXT NOT NULL UNIQUE,  -- 'person', 'tool', 'research_paper'
    label           TEXT NOT NULL,         -- human-readable name
    description     TEXT,                  -- what belongs in this kind
    created_at      TEXT NOT NULL,
    created_by      TEXT NOT NULL,         -- 'bootstrap:v1' | 'schema_evolver:vN' | 'operator'
    source_event_id TEXT REFERENCES events(id)
) STRICT;
-- + kind_registry_no_update / kind_registry_no_delete triggers (I2)

CREATE TABLE IF NOT EXISTS kind_revisions (
    id              TEXT PRIMARY KEY,
    action          TEXT NOT NULL CHECK (action IN
                        ('add','rename','merge','split','deprecate','restore')),
    from_slug       TEXT,                  -- NULL for 'add'
    to_slug         TEXT,                  -- NULL for 'deprecate'/'restore'
    detail          TEXT,                  -- JSON: e.g. split successor list + default
    revised_at      TEXT NOT NULL,
    revised_by      TEXT NOT NULL,
    reason          TEXT NOT NULL,
    source_event_id TEXT REFERENCES events(id)
) STRICT;
-- + kind_revisions_no_update / kind_revisions_no_delete triggers (I2)
```

**Bootstrap seed.** At init (idempotent `INSERT OR IGNORE`, new
`substrate/kinds.py` called from DB init), the current seven kinds are seeded
with `created_by='bootstrap:v1'`. This is exactly the "minimal bootstrap
scaffold" I6 permits: the seven survive as the starting point, not as law.

**Resolution, latest-row-wins.** The same pattern `tuner_state` already uses.
For a slug `s`:

- *Current successor:* follow the latest `kind_revisions` row with
  `from_slug = s` and `action IN ('rename','merge')` to its `to_slug`;
  repeat, depth-capped at 16 like `resolve_canonical`. A later
  `restore` row with `from_slug = s` terminates the chain at `s` (this is
  how a merge or rename is reversed: append the compensating revision,
  never touch the old rows).
- *Liveness:* a slug is live unless its latest lifecycle row
  (`deprecate` / `restore` / its own `add`) is `deprecate`.

Implemented as Python helpers in `substrate/kinds.py` mirroring the shape of
`resolve_canonical` / `resolve_canonical_batch`:
`resolve_kind_slug(conn, slug) -> str`, `live_kinds(conn) -> list[KindRow]`,
`resolve_kind_batch(conn, slugs) -> dict[str, str]`, plus one SQL view for
inspectability (`sqlite3` users can see the ontology without Python):

```sql
CREATE VIEW IF NOT EXISTS kind_current_v1 AS
SELECT k.slug,
       k.label,
       k.description,
       NOT EXISTS (
           SELECT 1 FROM kind_revisions r
           WHERE r.from_slug = k.slug
             AND r.id = (SELECT r2.id FROM kind_revisions r2
                         WHERE r2.from_slug = k.slug
                         ORDER BY r2.revised_at DESC, r2.id DESC LIMIT 1)
             AND r.action IN ('deprecate','rename','merge')
       ) AS is_live
FROM kind_registry k;
```

(Views are versioned by name, `_v1`; a changed definition ships as
`kind_current_v2`, never a redefinition, per I3.)

**The three enum sites become registry reads.**

- `entity_canonicalizer._normalize_kind` resolves against
  `live_kinds()` + the revision chain instead of `_VALID_KINDS`; the
  hardcoded variant map (`org → organization` etc.) stays as a
  deterministic first pass.
- `corrections.ENTITY_KINDS` validation becomes "slug resolves to a live
  registry kind" (parse, don't cast, unchanged in spirit).
- The extractor tool schema drops the `enum` on `entities.items.type` and
  mirrors `best_guess_kind`: free text with a description listing the
  *current live kinds* (rendered from the registry at prompt-build time) as
  preferred labels. The LLM may propose anything; normalization decides what
  lands.

**Free-text kinds get a ledger, not auto-registration.** A proposed kind that
does not resolve to a live slug is normalized deterministically (variant map,
else `other`) so the write path never blocks on ontology questions
(write-first intake). The raw proposal is preserved in a new append-only
table, which is the usage signal the Schema-Evolver mines:

```sql
CREATE TABLE IF NOT EXISTS kind_observations (
    id               TEXT PRIMARY KEY,
    raw_kind         TEXT NOT NULL,      -- what the extractor actually said
    normalized_slug  TEXT NOT NULL,      -- what the registry mapped it to
    entity_id        TEXT NOT NULL REFERENCES entities(id),
    event_id         TEXT NOT NULL REFERENCES events(id),
    observed_at      TEXT NOT NULL,
    observed_by      TEXT NOT NULL
) STRICT;
-- + append-only triggers; index on (raw_kind), (normalized_slug, observed_at)
```

Nothing is lost to the flattening: when `research_paper` shows up 40 times as
a raw kind squashed into `concept`, the evidence for promoting it is sitting
in this table.

### 2. Kind decouples from entity identity

**Analysis of the current scheme.** `entity_id(name, kind)` matters only at
*creation time*: it is the dedupe key for Stage-1 exact match and for
`write_entity` idempotency. Once created, the ID is an opaque hash; nothing in
the codebase parses the kind back out of it. Existing IDs therefore never need
to change: the fix is to stop feeding kind into the hash for *new* entities
and to stop treating `entities.kind` as authoritative on *reads*.

**New identity scheme (v2), new entities only:**

```
entity:v2:<sha256(lowercase(canonical_name) + "|" + disambiguator)>
```

where `disambiguator` is an ordinal string, `"0"` by default. It increments
only when the system *deliberately* splits homonyms: the LLM judge (or the
operator) rules that a new mention of "Apple" is a different thing from every
existing live "Apple", and the new entity gets the next ordinal
(`count of existing entity_identities rows for that name_lower`). The
derivation is a pure function of prior graph state, so a rebuild that replays
the same canonical decisions in the same event order reproduces the same IDs
(the property the v1 scheme had). Each v2 identity is recorded for
introspection and for the ordinal computation:

```sql
CREATE TABLE IF NOT EXISTS entity_identities (
    entity_id      TEXT PRIMARY KEY REFERENCES entities(id),
    name_lower     TEXT NOT NULL,
    disambiguator  TEXT NOT NULL,
    id_scheme      TEXT NOT NULL,     -- 'v1' rows may be backfilled lazily; 'v2' always written
    created_at     TEXT NOT NULL,
    UNIQUE(name_lower, disambiguator, id_scheme)
) STRICT;
-- + append-only triggers (I2)
```

(`entities` cannot gain a column: `SCHEMA_DDL` is a tuple of idempotent
statements and SQLite has no `ADD COLUMN IF NOT EXISTS`, so the additive-only
rule pushes new fields into new tables. The `entities.kind` column stays
`NOT NULL` and, for v2 rows, records the *initial* kind signal at creation,
nothing more.)

**Kind becomes a mutable-by-append derived attribute.** The exact overlay
pattern `entity_retractions` and `merge_invalidations` already use:

```sql
CREATE TABLE IF NOT EXISTS entity_kind_assignments (
    id              TEXT PRIMARY KEY,
    entity_id       TEXT NOT NULL REFERENCES entities(id),
    kind_slug       TEXT NOT NULL,
    assigned_at     TEXT NOT NULL,
    assigned_by     TEXT NOT NULL,     -- 'operator' | 'schema_evolver:vN' | worker id
    confidence      REAL NOT NULL,
    reason          TEXT NOT NULL,
    source_event_id TEXT REFERENCES events(id)
) STRICT;
-- + append-only triggers (I2); index on (entity_id, assigned_at DESC)
```

**Resolution view: the backward-compatible read path.** An entity's current
kind is its latest assignment, falling back to the immutable `entities.kind`
that every existing row already carries. That fallback *is* the backfill: zero
rows are rewritten, zero rows are copied, old vaults resolve identically until
the first assignment overlays a row.

```sql
CREATE VIEW IF NOT EXISTS entity_current_kind_v1 AS
SELECT e.id AS entity_id,
       COALESCE(
           (SELECT ka.kind_slug FROM entity_kind_assignments ka
            WHERE ka.entity_id = e.id
            ORDER BY ka.assigned_at DESC, ka.id DESC LIMIT 1),
           e.kind
       ) AS kind_slug
FROM entities e;
```

The hot path uses a Python batch helper (`resolve_entity_kind_batch`,
mirroring `resolve_canonical_batch`, one query per recall) that additionally
pipes the resulting slug through `resolve_kind_slug` so a *registry-level*
merge (`organization → company`) retypes every affected entity at read time
with a single revision row and no per-entity writes. Full resolution order:

```
entities.kind  →  latest entity_kind_assignments row (if any)
               →  kind_revisions chain (rename/merge, latest-row-wins)
               →  the slug recall serves
```

**What this simplifies.**

- `retype_entity` stops being a merge. A retype is one
  `entity_kind_assignments` row anchored to an `observe` event. No fresh
  entity, no merge chain, no `_retype_merged_entity` cycle dance (a revert is
  just another assignment row). The merge-based path stays in the code for
  reading history and for v1 vaults mid-transition, marked deprecated.
- The deduplicator's main workload (same-name cross-kind splits) cannot occur
  for v2 entities, because kind no longer forks identity. The worker remains
  to drain the v1 backlog and to catch genuine spelling variants.

**Creation-time matching under v2.** Stage-1 exact match becomes name-first
with a kind guard:

1. Look up live entities by `LOWER(canonical_name)` (kind-free).
2. Exactly one candidate AND (its resolved kind equals the normalized proposed
   kind, OR either side is `other`): link, `match_method='exact'`. This
   preserves today's homonym precision: "Apple" the company and "apple" the
   concept still reach the LLM instead of auto-linking.
3. Multiple same-name candidates, or a kind disagreement: Stage 2 LLM
   judgment with *all* same-name entities in the candidate menu (each shown
   with its resolved kind). "None of these" creates a new identity with the
   next disambiguator ordinal.
4. Stage 1.5 gazetteer keys switch from `(kind, alias)` to
   `(resolved_kind, alias)`; the candidate pool filter switches from
   `entities.kind` to the resolution view.

### 3. The Schema-Evolver cold-path agent

A real `ColdPathWorker` (`afair/agents/schema_evolver.py`), registered in the
`build_server` worker list next to `EntityAuditWorker`. VISION §6.5 finally
gets its named agent. **It proposes; it never applies.** Application goes
through the operator, always (see the confirm loop below).

```python
class SchemaEvolver(ColdPathWorker):
    name = "schema_evolver"
    interval_seconds = 24 * 3600   # ontology changes slowly; daily is plenty
```

Model: `settings.schema_evolver_model`, resolved like the other per-agent
overrides (blank falls back to `extractor_model`); VISION §6.5 earmarks this
worker for a premium model, so the docs recommend a Sonnet-class override.

**Signals (deterministic SQL, no LLM):**

| Signal | Query shape | Proposal it feeds |
|---|---|---|
| Kind usage distribution | live-entity + mention counts per resolved kind via the resolution view | context for every proposal |
| Over-broad `other` | `other` holds more than `OTHER_SHARE_THRESHOLD` (default 0.20) of live entities | `promote` (carve a new kind out of it) |
| Frequent free-text kind | one `raw_kind` in `kind_observations` normalized away on ≥ `PROMOTE_MIN_ENTITIES` (default 10) distinct entities over ≥ 14 days | `add` + per-entity reassignment list |
| Near-duplicate kinds | two live kinds whose entities co-occur in `kind_observations` (same entity observed under both raw kinds), or whose raw-kind strings are lexical variants | `merge` |
| Unused kind | zero live entities for ≥ 90 days (and not a bootstrap kind in its first 90 days) | `deprecate` |
| Overloaded kind | a kind whose sampled entities the LLM judges to be two coherent sub-populations (only checked for the top-1 largest kind per cycle) | `split` |

**LLM role, deterministically fenced.** The signals produce a compact summary
plus entity-name samples (wrapped with `wrap_untrusted`, per the existing
prompt-injection discipline). One `call_tool` call per candidate proposal
drafts the human-facing part: new slug, label, one-paragraph description,
which sampled entities move. Deterministic backstops validate everything the
LLM returns before a proposal row is written:

- slug is `^[a-z][a-z0-9_]{1,30}$` and does not collide with any registry row;
- every entity ID in a reassignment list was in the sample shown to the model
  (the same candidate-set binding the canonicalizer applies, Security L1);
- reassignment lists are capped (`MAX_REASSIGN_PER_PROPOSAL`, default 50);
- at most `MAX_PROPOSALS_PER_CYCLE` (default 2) and
  `MAX_LLM_CALLS_PER_CYCLE` (default 4) per run;
- per-kind cooldown: no new proposal touching a slug that had a revision or a
  rejected proposal in the last 30 days (anti-thrash; the ADHD mode-switching
  failure from VISION §5.2 is the cautionary case for an over-eager evolver).

**Quarantine queue.** `proposed_corrections` cannot host these: its `CHECK
(kind IN ('retype','merge','merge_review'))` cannot be widened in place and
its rows are entity-keyed. A parallel mutable queue mirrors its documented
exception (regenerable derived state, no I2 trigger, deciding is the only
mutation):

```sql
CREATE TABLE IF NOT EXISTS proposed_ontology_revisions (
    id            TEXT PRIMARY KEY,
    action        TEXT NOT NULL CHECK (action IN
                      ('add','rename','merge','split','deprecate')),
    subject_slug  TEXT NOT NULL,        -- the kind being revised ('' for pure add)
    detail        TEXT NOT NULL,        -- JSON: new slug/label/description,
                                        -- successor slugs, entity reassignment list
    evidence      TEXT NOT NULL,        -- the deterministic signal, human-readable
    confidence    REAL NOT NULL,
    detected_by   TEXT NOT NULL,
    detected_at   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'proposed'
                  CHECK (status IN ('proposed','confirmed','rejected','applied')),
    decided_at    TEXT,
    decided_by    TEXT,
    UNIQUE(action, subject_slug)
) STRICT;
```

**Human-in-the-loop via the existing decide loop (I1-safe).** Ontology
proposals join `pending_corrections` in `recall(stats=True)` and the
`afair://session-start` resource, each with a ready-to-ask prompt ("The
extractor has wanted a 'research_paper' kind 40 times; 12 entities now filed
as 'concept' would move. Add it?"). The operator's verdict arrives through the
existing `recall(decide={proposal_id, verdict})`; `decide_correction`
dispatches on an ID prefix (`ont_...` vs the entity-proposal ULIDs) to an
ontology-apply path. No new verb, no changed signature, additive payload
fields only, exactly the ADR-0002 pattern.

**Applying a confirmed revision (all append-only):**

1. Write an `observe` event (`action="apply_ontology_revision"`, the proposal
   ID as subject): the I7 anchor every downstream row references via
   `source_event_id`.
2. `add` / `promote`: insert the `kind_registry` row + an `add` revision row;
   then one `entity_kind_assignments` row per entity in the proposal's
   (bounded, validated) reassignment list.
3. `rename` / `merge`: insert the successor registry row if new + the revision
   row. Zero per-entity writes; resolution does the rest at read time.
4. `deprecate`: the revision row; the registry row stays forever (history).
5. `split`: insert successor kinds, write per-entity assignments for the
   proposal's classified list, then the `split` revision rows (one per
   successor, `detail` marks the default successor for any straggler entity
   that gained no explicit assignment).
6. Stamp a `pipeline_events` row (`stage='schema_evolver.applied'`) and flip
   the queue row to `applied`.

**Reversal (I7).** Every action has a compensating append: `add` is reversed
by `deprecate`; `deprecate` by `restore`; `rename`/`merge` by `restore` on the
from-slug (latest-row-wins ends the chain there); reassignments by newer
assignment rows. Nothing is ever un-written. The evolver's own proposals,
verdicts, anchors, and revisions reconstruct "why does the ontology look like
this at time T" completely.

### Invariant compliance

- **I1 (MCP surface stability).** No new tool, no signature change. Proposals
  ride `recall(stats=True)` / `afair://session-start` as additive payload
  fields; verdicts ride the existing `recall(decide=)`; anchors are ordinary
  `observe` events. A v1.0 client that never sends `decide` keeps working
  unchanged.
- **I2 (substrate immutability).** Every new belief-bearing table
  (`kind_registry`, `kind_revisions`, `kind_observations`,
  `entity_kind_assignments`, `entity_identities`) carries the standard
  no-update/no-delete trigger pair. Revision and reversal are new rows.
  `proposed_ontology_revisions` is mutable by the same documented exception as
  `proposed_corrections`: a regenerable suggestion queue, not a belief.
- **I3 (backward-compatible evolution).** No `ALTER`, no rewrite, no data
  backfill. Existing kind-in-ID entities keep their IDs forever and resolve
  through the `COALESCE(..., entities.kind)` fallback, so an untouched old
  vault reads bit-identically. New behavior is new tables plus versioned views
  over unchanged rows. A full graph *rebuild* from substrate under v2 rules
  yields v2 IDs; that is a new interpretation version over the unchanged event
  log (exactly what I3 prescribes), and the old graph tables remain readable
  beside it.
- **I6 (emergent over imposed).** This is the ADR that discharges the debt
  recorded in the I6 text. Kinds become data with a full revision lifecycle
  (add/rename/merge/split/deprecate/restore), driven by observed usage, with
  the current seven surviving only as the bootstrap seed. The parenthetical in
  VISION §4 I6 gets deleted when Phase 4 ships.
- **I7 (recorded + reversible self-modification).** Every revision is anchored
  to an `observe` event, logged in append-only tables, and reversible via
  compensating rows. The Schema-Evolver can only *propose*; the operator gate
  means the self-modifying loop cannot drift unsupervised (ADR-0002's "AI
  suggests, operator confirms" applied to the ontology itself). I1-I6 stay
  exempt: the evolver has no write path to the MCP surface, the event log, or
  its own guardrails; its tunables live in the tunable registry like every
  other worker's.
- **I5 (vendor neutrality), untouched but honored:** the evolver calls
  `call_tool` through the litellm wrapper with an env-selected model, like
  every other agent.

## Consequences

- Entity kinds stop being law and become beliefs: assigned by append,
  resolved by view, revised by usage, confirmed by the operator. The
  "cognitive fingerprint" claim in VISION §10.4 becomes true at the entity
  layer, not just the event layer.
- Retype collapses from merge-chain surgery to a single assignment row; the
  `merge_review` cycle-avoidance complexity becomes legacy-only; the
  deduplicator's cross-kind workload dries up for new entities.
- A registry-level kind merge is O(1) rows instead of O(entities); ontology
  revision becomes cheap enough that the Schema-Evolver can actually do it.
- `kind_observations` gives the self-improvement loop another ground-truth
  stream: operator verdicts on ontology proposals are labeled data about what
  categorization fits this user, the same double-duty ADR-0002's edge reviews
  serve for the tuner.
- Cost of the design: reads gain one resolution hop (latest assignment +
  revision chain). Mitigated the same way `resolve_canonical_batch` was:
  batch helpers, one query per recall, indexes on the new tables. The
  latest-row subquery shape is already proven on `entity_merges`.
- Risk: homonym precision now leans on the Stage-1 kind guard + LLM stage
  instead of kind-forked identity. The guard reproduces today's behavior for
  the known cases, and a false link remains reversible through the existing
  merge-invalidation machinery, but this is the spot to watch in dogfooding.
- Risk: ontology thrash (rename ping-pong, over-eager splits). Contained by
  per-kind cooldowns, per-cycle caps, and the operator gate; a noisy evolver
  costs review attention, never data.
- Risk: prompt-injected ontology (an attacker-crafted event proposing a
  poisoned kind at scale). Contained by: free-text kinds never auto-register;
  proposals are drafted only from deterministic aggregate signals; slugs are
  format-validated; reassignment lists are bound to the shown sample; and
  nothing lands without the operator.

## Alternatives considered

- **Leave the enum fixed; soften I6 to "event-level typing only."** Rejected.
  ADR-0001's stance is that the evolution is wrong, not the invariant, and
  here it is the *code* that is wrong, not the invariant. The entity graph is
  the flagship emergent surface; exempting it guts the differentiation claim
  (VISION §9.6.D).
- **No kinds at all (fully free-form entities).** Rejected. Kinds earn their
  keep operationally: candidate-pool pruning, homonym disambiguation, audit
  heuristics, article prompts. I6 demands *revisable*, not *absent*;
  deleting the dimension trades an imposed ontology for no ontology, which is
  the aphantasia of entity graphs: a fallback, not a goal.
- **Keep kind-in-ID and overlay "alias kinds."** Analyzed and rejected. Every
  retype stays an identity change with a merge chain; a registry-level merge
  still forces O(entities) new identities + merges; `resolve_canonical`
  chains grow with every ontology revision and the 16-hop depth cap becomes
  reachable. The overlay treats the symptom while the identity function keeps
  regenerating the disease.
- **Mutable `kind` column on `entities`.** Rejected outright: the table
  carries I2 no-update triggers, and in-place mutation would erase the
  revision history I7 requires.
- **Store the ontology in `ontology/versions/` files (per the VISION §6.3
  vault layout sketch).** Rejected for now: the resolution must join against
  entity reads inside SQLite; a file-based registry would need a sync
  mechanism and could drift from the DB. The registry tables *are* the
  ontology state; an exporter can materialize them into `ontology/` later
  without changing the source of truth.
- **Auto-apply high-confidence evolver proposals.** Rejected per ADR-0002:
  ontology revisions are high-blast-radius (they re-label whole populations at
  read time), which is precisely the class of change that gets quarantined.
  Auto-confirm can be revisited once verdict history exists to calibrate
  against, through the tuner, with its own ADR.

## Implementation phases

Each phase is independently shippable, green, and reversible. No phase
rewrites data; rollback is always "revert the commit," because the new tables
are additive and inert without the code that reads them.

**Phase 1: kind registry (behavior-preserving).**
Ships: `kind_registry` + `kind_revisions` + `kind_observations` DDL with
triggers, `kind_current_v1` view, `substrate/kinds.py` (seed +
resolve/live/batch helpers), bootstrap seed of the current seven; the three
enum sites re-pointed at the registry (canonicalizer normalize, corrections
validation, extractor prompt enum rendered from live kinds).
Changes for users: none; the live kind set is still exactly the seven.
Tests: seed idempotency (double init, no dupes); resolution over
rename/merge/deprecate/restore chains incl. latest-row-wins reversal and the
16-hop cap; `_normalize_kind` parity against the current variant map;
`decide_correction` accepts registry kinds and rejects dead slugs; I2 triggers
fire on UPDATE/DELETE of the new tables.
Rollback: revert; seeded rows are harmless.

**Phase 2: decouple kind from identity.**
Ships: `entity_kind_assignments` + `entity_identities` DDL,
`entity_current_kind_v1` view, `resolve_entity_kind_batch`, the v2 ID scheme
in `entities.entity_id` (new `entity_id_v2(name, disambiguator)`; the v1
function stays for reads/tests), name-first Stage-1 with the kind guard,
gazetteer/candidate-pool switched to resolved kinds, retype rewired to an
assignment row, recall/articles/audit/dedup reading resolved kinds.
Changes: new entities get v2 IDs; retype stops growing merge chains; v1
entities are retypeable by assignment without identity change.
Tests: old-vault fixture resolves identically pre-assignment (the COALESCE
path); v1 entity kind override via one assignment row, ID unchanged;
homonym split determinism (same replay, same ordinals, same IDs); Stage-1
guard keeps "Apple company vs apple concept" apart; recall payload carries the
resolved kind; retype revert via a second assignment.
Rollback: revert; already-written v2 entities and assignments remain valid
rows that the reverted code simply reads via `entities.kind` fallback
(assignment overlay ignored, no corruption).

**Phase 3: free-text kinds from the extractor.**
Ships: extractor tool schema `type` un-enumed (free text + live-kind
description), canonicalizer records every normalization into
`kind_observations`.
Changes: unknown kinds still land as `other` (or the variant map's target),
but the raw signal is now retained.
Tests: unknown raw kind writes an observation row and normalizes
deterministically; known slugs and variants bypass `other`; extraction
round-trip with the loosened schema on all content types.
Rollback: revert; observation rows are inert history.

**Phase 4: Schema-Evolver, propose-only.**
Ships: `proposed_ontology_revisions` DDL, `agents/schema_evolver.py` (signals,
LLM drafting, deterministic backstops, caps, cooldowns),
`settings.schema_evolver_model`, worker registered in `build_server`,
tunables in the tunable registry, `pipeline_events` cycle marker.
Changes: proposals accumulate in the queue; nothing is applied; the live
ontology is untouched.
Tests: each signal detector against fixture vaults (over-broad other,
frequent raw kind, co-occurring duplicate pair, unused kind); slug and
sample-binding backstops reject malformed LLM output; `UNIQUE(action,
subject_slug)` + cooldown make re-runs no-ops; caps hold.
Rollback: unregister the worker; queue rows are inert.

**Phase 5: operator-confirm application.**
Ships: ontology proposals in `read_pending_corrections` output (prefix-keyed
IDs), `recall(stats=True)` + session-start surfacing, the apply path in
`corrections.py` (anchor event, registry/revision/assignment writes, statuses,
idempotent double-decide), reversal helpers (`restore`, compensating
assignments), VISION §4 I6 parenthetical removed, CLAUDE.md status updated.
Tests: end-to-end confirm of each action flips the resolved view as specified
(merge retypes at read time with zero per-entity writes; split default
successor covers stragglers); reject leaves the live ontology unchanged;
double-decide reports `already_decided`; restore reverses a merge; every
applied revision has its anchor `observe` event.
Rollback: revert the apply path; already-applied revisions stay historical
and remain reversible through `restore` rows, per I7.

## As built (2026-07-02)

All five phases shipped and released. The commit chain on `main`:

- `3f7dd8b` Phase 1 (kind registry) — released in **v0.1.5**
- `1434896` Phase 2 (decouple kind from identity: v2 IDs, mutable kinds,
  homonym guard) — **v0.1.5**
- `8e58ed1` Phase 3 (free-text kinds + `kind_observations` ledger) — **v0.1.5**
- `1336683` Phase 4 (Schema-Evolver, propose-only) — **v0.1.5**
- `b718ab1` Phase 5 (operator-confirmed ontology revisions) — **v0.1.5**
- `accb38d` dedup defers to operator-decided merges (the Graphiti re-merge
  cycle fix) — **v0.1.6**

### Phase 2 completion (making decoupling effective on the live vault)

Post-ship, a live-vault checkup found the v1-era backlog (same-name clusters,
all cross-kind because the v1 hash made same-name-same-kind impossible) still
present, and identified a residual formation path. Six slices closed the gaps
(the ADR's own re-examination discipline). All are behavior-preserving except
where noted, and each is `git revert`-able (no slice rewrites substrate rows):

- **Slice 1** — `scripts/checkup_entities.py`: a strictly read-only diagnostic
  (identity-scheme census, same-name cluster census, formation/drain rates,
  and the `_kinds_agree` `other`-wildcard metric). Measure before treating.
- **Slice 2** — canonicalizer defers events when the per-cycle LLM budget is
  exhausted instead of draining them exact-only. Exact-only drain was the
  residual formation path: a kind flip on an existing name with no LLM minted
  a new same-name cross-kind v2 duplicate. Deferred events keep zero mentions
  and re-surface next cycle. New stat `events_deferred_no_budget`.
- **Slice 3** — the deduplicator writes `assign_entity_kind` rows unifying a
  confidently same-entity cluster's kind (>= `KIND_UNIFY_CONFIDENCE` = 0.9,
  candidate-set bound to the shown members' kinds, I6). A unified cluster
  shows equal kinds on both sides of the merge, so `entity_audit` files no
  `merge_review` — kind disagreements become kind revisions, not review debt.
- **Slice 4** — the deduplicator skips a recorded deliberate homonym split
  (>= 2 v2 disambiguators for a name, all members v2 split identities); a
  cluster with a v1 leftover is still judged.
- **Slice 5** — `scripts/drain_entity_dedup.py`: a supervised operator tool
  that loops `EntityDeduplicator.run()` at a raised per-cycle cap to work the
  backlog down in batches (`--dry-run`, `--max-clusters`, `--sleep`); writes
  an `observe` audit anchor (I7). Reuses `run()`, so it inherits every guard.
- **Slice 6** — this note; `docs/self-hosting.md` entity-graph runbook.

Consequence for the deduplicator: for v2 entities its cross-kind workload
dries up structurally (a genuine same-thing kind-mislabel now links to the
existing entity at canonicalization time rather than forking identity); what
remains is the v1 backlog and genuine spelling variants, as intended.
