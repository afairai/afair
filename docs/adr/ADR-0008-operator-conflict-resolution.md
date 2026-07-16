# ADR-0008: Operator conflict resolution through the correction loop

> **Status:** Accepted
> **Date:** 2026-07-16
> **Audience:** anyone touching the conflict resolver, `decide_correction`, the pending-corrections queue, recall serving of conflicts, or the dashboard decide path
> **Relates to:** VISION.md §4 (I1, I2, I3, I4, I7, I8), [ADR-0002](ADR-0002-belief-revision-derived-layer.md) (operator-in-the-loop, the single mutation point), [ADR-0003](ADR-0003-emergent-ontology.md) (the `ont_` prefix-dispatch precedent), [ADR-0004](ADR-0004-edge-confidence-model.md) (serve with a caveat, never suppress), [ADR-0005](ADR-0005-telemetry-retention.md) (the memory-vs-operational line the new queue sits on)

## Context

The conflict resolver deliberately never auto-invalidates. When two events
contradict, it writes a `conflict_flag` interpretation, recall serves the
tension alongside hits, and the destructive choice of which side stands is
left to a human. That stance is correct and stays. It also created a dead
end: the Memory Mirror projects unresolved conflicts onto a dashboard, so the
operator can now SEE every live contradiction in the vault, with no way to
act on any of them. A flag, once raised, stayed raised forever unless the
operator hand-crafted an invalidation through a client.

Three shipped mechanisms already describe where resolution belongs:

- **ADR-0002** established "AI suggests, operator confirms" with
  `decide_correction` as the single mutation point for every operator
  decision. Nothing that changes derived belief bypasses it.
- **ADR-0003 Phase 5** established the sub-queue pattern on that same entry:
  ontology proposals live in their own store, carry an `ont_` id prefix, and
  `decide_correction` dispatches on the prefix. One verb, one argument, two
  queues.
- **ADR-0004** established the serving posture for contested material: serve
  it with a caveat attached, never suppress it.

A dashboard button that confirms a proposal is not new semantics. It is the
ADR-0002 operator-in-the-loop reached over a second transport, and it must
land in the identical substrate writes as the same decision made through
`recall(decide=)`. The design question is therefore only how conflicts enter
the existing confirmation loop, not whether to build a second one.

## Decision

Unresolved conflict flags become decidable proposals, decided through the
one mutation point that already exists, resolved by append-only substrate
records, expressed in the frozen verdict vocabulary, and served afterwards
with their resolution attached. Five parts:

### 1. A non-substrate proposal queue

A new `proposed_conflict_resolutions` table holds one row per undecided
conflict pair: an id with the `cfl_` prefix (prefix plus ULID), a canonical
`pair_key` (the two content hashes ordered and joined), both event ids and
hashes, which hash is newer, the flag's verdict, reason, and confidence,
detection provenance, a `status` of `proposed`, `applied`, or `rejected`, a
`resolution` of `superseded_older`, `superseded_newer`, or `no_conflict`,
and the decision stamp. `status` is indexed; a partial unique index on
`pair_key` where `status = 'proposed'` prevents duplicate open proposals.

The resolver enqueues a proposal right after writing an unresolved
`conflict_flag`, guarded by a pair-key existence check so a re-judged pair
never re-nags. A bounded backfill (at most 50 pairs per cycle) walks
historical unresolved flags, so every conflict raised before this ADR
becomes decidable too.

The queue is operational state, not memory, on the exact line ADR-0005 drew:
a decision mutates `status`, so the table carries no append-only triggers,
is regenerable from the flags and resolutions it points at, is excluded from
the vault export, and the Pruner deletes decided rows past retention.

### 2. One mutation point, two transports

Conflict proposals are decided ONLY through `decide_correction`. An id
carrying the `cfl_` prefix dispatches to the conflict decide path, symmetric
with the shipped `ont_` dispatch. Because the dispatch lives inside the
function every caller already uses, both transports get conflicts for free:
`recall(decide=)` over MCP, and the dashboard's `POST /internal/decide`
route. The route validates its input and calls `decide_correction`; it runs
zero SQL of its own against any queue, so the two transports cannot drift.
Ownership is structural: a single-tenant vault behind credential-gated
paths (I8), so the only person who can reach either transport is the vault's
operator.

### 3. Resolution is append-only

Deciding a conflict writes new records and mutates none:

- **A resolution interpretation, always.** One per pair, with
  `produced_by = "conflict_resolution:v1:<event_b_hash>"` and an extraction
  carrying the content type, both event hashes and ids, the resolution, the
  proposal id, the invalidation event id (or null), and the decision stamp.
  Interpretations are substrate: I2 triggers apply and the record rides the
  vault export. The existing uniqueness backstop on
  `(event_hash, version, produced_by)` prevents duplicates.
- **An `invalidate` event, when a side is superseded.** `write_invalidation`
  appends a new event whose `parent_hashes` carries the losing hash; the
  losing event is never touched (I2).
- **An observe event, always.** The decision itself lands in the vault's own
  record: who decided, which pair, which way (I7).

The source events and the original `conflict_flag` interpretation stay
byte-identical forever. Write order is substrate first, queue status last,
so a crash mid-decision leaves a re-decidable proposal rather than a lost
resolution. The path is idempotent: an already-decided proposal reports its
prior status, an unknown id reports not-found, and when the losing side was
already invalidated the duplicate invalidation is skipped, the resolution is
still written, and the outcome notes it.

### 4. Three intents, the frozen verdict enum

The operator has three possible intents on a conflict. They map onto the
shipped `confirm` / `reject` / `retract` verdicts through directional
framing: the proposal's prompt states the direction (newer versus older),
and the verdicts keep their plain meanings relative to it. No wire schema
changes.

| Operator intent | Verdict | Substrate writes | Queue status |
|---|---|---|---|
| Keep newer (the newer fact is current) | `confirm` | invalidate the OLDER event, resolution `superseded_older`, observe | `applied` |
| Not a conflict (both stand) | `reject` | resolution `no_conflict`, observe, no invalidation | `rejected` |
| Keep older (the newer fact is wrong) | `retract` | invalidate the NEWER event, resolution `superseded_newer`, observe | `applied` |

`revert` and `to_kind` on a `cfl_` id are validation errors: there is no
kind to correct and nothing to revert that re-validation of the invalidated
event does not already cover.

### 5. Served with the resolution, excluded from unresolved counts

The conflict read path gains a companion pass over the
`conflict_resolution:v1` producers, attaching each flag's resolution or
null. Resolved flags are served WITH their resolution (the ADR-0004
posture: annotate, never suppress), while the unresolved conflict count,
recall's conflict coverage, and the serving trim gate all share the same
resolution-is-null guard, so counts drop as decisions land. Pending views
merge open conflict proposals into the existing correction list as entries
of kind `conflict` with a directional prompt and empty entity fields (the
view's kind is a free string on the wire), and the pending count includes
the queue's `proposed` rows via an indexed count, not a derived scan.

## Consequences

- Every conflict the Mirror shows becomes decidable, including all
  historical flags (the backfill), from both the dashboard and any MCP
  client.
- The single mutation point holds. Transport-parity tests can assert that
  the same decision through MCP and through the route produces
  byte-identical substrate records.
- The full story of a contradiction is durable: the flag, the proposal, the
  resolution record, any invalidation, and the observe trail. A wrong
  decision is recoverable, because an invalidation is itself an append-only
  event that a later event can supersede (re-validation).
- The frozen wire surface does not change. The golden surface diff is a
  documentation addition and must remain additive.
- Two maintenance rules to keep: HTTP routes never run SQL against a
  proposal queue directly (the substrate function is the only writer), and
  resolution logic appends rather than mutates (the I2 triggers on
  interpretations and events enforce this; the tests assert the flag and
  both sources stay byte-identical across every verdict).

## Alternatives considered

**Insert literal `proposed_corrections` rows.** The existing table assumes
an entity subject: `entity_id` is NOT NULL with a foreign key into
`entities`, the reads join through it, and open-proposal uniqueness is per
kind and entity. A conflict's subject is a pair of events, not an entity.
Bending the table around that (sentinel entities, relaxed constraints)
would weaken its guarantees for every existing queue kind. Rejected; the
ontology queue already proved that a sibling store behind the same decide
entry costs less than contorting the shared table.

**A separate decide path for conflicts.** A parallel write semantics with
its own endpoint, idempotency rules, and audit shape. It would be
unreachable from `recall(decide=)`, so an MCP client could see conflicts it
cannot resolve, and it would fork the single mutation point ADR-0002 exists
to defend. Every future consumer would need to know two confirmation
systems. Rejected.

**Leave flags informational.** The status quo. Nothing retires a flag,
because the resolver's no-auto-invalidate stance is deliberate; any
"self-resolving" variant would mean machine judgment making exactly the
destructive choice the resolver refuses to make. Meanwhile the Mirror keeps
showing a growing pile the operator can see and not touch. Rejected.

**Widen the verdict enum with conflict-specific verbs.** Adding
`keep_newer` / `keep_older` / `no_conflict` as first-class verdicts. The
verdict vocabulary is part of the shipped `recall(decide=)` contract and is
shared by every sub-queue; a per-queue verb grows the advertised schema
with every feature and teaches clients a different vocabulary per proposal
kind. Directional framing expresses all three intents with the existing
verbs and zero wire change. Rejected.

## Invariant fit

- **I1**: no tool is added, removed, or re-typed. The three intents ride the
  existing `decide` argument and verdict values; the only golden-surface
  movement is documentation, and the diff must be additive.
- **I2**: the decision writes a new interpretation, optionally a new
  `invalidate` event, and an observe event; the sources, the flag, and any
  prior invalidations are never mutated. The queue itself sits on the
  operational side of the ADR-0005 line: mutable status, no I2 triggers,
  regenerable, prunable after decision.
- **I3**: resolutions attach as a companion read over unchanged flags; no
  migration runs, and pre-existing flags become decidable through the
  bounded backfill, not through rewriting.
- **I4 / I8**: only the vault's operator can decide, over credential-gated
  paths on a single-tenant vault. The durable outcome (interpretations,
  events) rides the user's export; the regenerable queue does not.
- **I5**: the decision path is deterministic and makes no model call.
- **I6**: no ontology or kind logic is touched; `to_kind` is explicitly
  rejected on conflict proposals.
- **I7**: every decision is recorded (the observe event plus the resolution
  record) and reversible (supersede the invalidation, decide again on the
  record).
