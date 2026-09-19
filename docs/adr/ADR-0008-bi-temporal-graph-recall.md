# ADR-0008: Bi-temporal graph memory without a second graph store

> **Status:** Accepted
> **Date:** 2026-08-03
> **Audience:** anyone touching entity edges, temporal inference, historical recall, export, or graph retrieval
> **Relates to:** VISION.md §4 (I1-I8), [ADR-0002](ADR-0002-belief-revision-derived-layer.md), [ADR-0004](ADR-0004-edge-confidence-model.md), [ADR-0007](ADR-0007-emergent-living-syntheses.md)

## Context

afair already has the stronger constitutional substrate: one user-owned SQLite
vault, immutable source events, append-only correction ledgers, confidence with
provenance, emergent ontology, and cited living syntheses. Replacing it with
Graphiti, or adding Graphiti's Neo4j/FalkorDB store beside it, would create two
competing memories and weaken invariants I2-I5.

Graphiti nevertheless has two architectural ideas worth adapting:

1. every fact distinguishes world time (`valid_at` / `invalid_at`) from
   knowledge time (`created_at` / `expired_at`);
2. the graph participates in retrieval through bounded traversal and diversity
   ranking instead of appearing only after a text hit.

The final comparison used Graphiti 0.29.3 (Apache-2.0) at commit
`899cb40d043b3f085917a69d95f26ed5ea24f411` (2026-08-03), including
`edges.py`, `search/search.py`, `search_config.py`, the search recipes, and
`utils/maintenance/edge_operations.py`. No Graphiti source file is copied and
no runtime dependency is introduced. The adapted patterns are independently
implemented for afair's AGPLv3, append-only SQLite design.

## Decision

afair remains the only primary memory and only graph store. We implement the
Graphiti-derived strengths as additive projections inside the existing vault.

### A. Two clocks per entity fact

`entity_edges.valid_from` and `valid_to` remain the immutable at-discovery
snapshot. A new append-only `edge_validity_spans` table records later temporal
interpretations:

- `valid_from` / `valid_to`: when the fact was true in the world;
- `recorded_at`: when afair learned this interpretation;
- `recorded_by`, `source_event_id`, `confidence`, `reason`: provenance and
  uncertainty.

The latest `(recorded_at, id)` row composes the effective validity view. A
new worker version appends a new row; it never mutates the prior belief. Edge
invalidation remains a separate append-only knowledge-time expiry signal.

The two clocks are deliberately queryable independently:

- `edge_is_valid_at`: world-time interval only;
- `edge_was_known_at`: discovered/expired knowledge time only;
- `edge_visible_as_of`: both lenses at the same historical point.

### B. Worker-order independence

Cold-path workers are asynchronous. Validity must converge whether the entity
canonicalizer or temporal worker runs first:

- canonicalizer first: the edge receives the source event's recorded time as a
  low-confidence reference-time fallback; temporal classification later
  appends a refined span;
- temporal worker first: edge creation reads the latest event-temporal result
  and writes the refined initial span directly.

Legacy edges are upgraded by the bounded, idempotent
`scripts/backfill_edge_validity.py` maintenance command. It makes no model
calls and journals the completed run with an actor/model signature.

Malformed model-produced dates fail soft and retain the reference-time
fallback. Explicit caller-provided intervals are validated strictly before the
edge is written.

For a `one_off` event only, `relevance_horizon` may act as `valid_to`. For other
classes it is not treated as fact expiry; relevance and truth are different.

### C. Bounded graph retrieval and MMR

The read-path companion to this ADR adds a deterministic, bounded one-to-two
hop traversal as another recall candidate arm. Graph-reached events are
appended to the direct text/vector candidate pool; the subsequent maximal
marginal relevance pass can then promote a distinct graph result above
near-duplicate direct hits. Hard fanout/edge/event caps and hub suppression
prevent a high-degree entity from flooding recall without a new model or heavy
dependency.

This is a read projection, not a new tool or database. Existing recall behavior
remains available and the three frozen MCP verbs do not change.

An additive `recall(as_of="<ISO-8601>")` argument exposes the useful part of
Graphiti's temporal query model at the MCP boundary. It promotes the default
`depth="auto"` to `deep`, normalizes the instant to UTC, and runs graph
traversal against that world-time lens. Explicit shallow/normal calls are
rejected rather than silently ignoring the date. Knowledge-time invalidations
remain authoritative.

Graphiti's configurable RRF/MMR/node-distance/episode-mentions/cross-encoder
recipes were reviewed in source. afair keeps its existing FTS5 + sqlite-vec +
RRF baseline, independently written lexical MMR, and bounded graph walk. A
cross-encoder was not adopted: it would add another model call to every deep
recall, while afair's measured problem was duplicate results, not missing a
semantic judge. Search profiles are already represented by afair's shallow /
normal / deep contract, so a second recipe abstraction would be duplication.

### D. Export completeness

`edge_validity_spans` is part of the user-owned export. Some rows can be
re-derived from event temporal metadata, but explicit human corrections cannot;
exporting the complete ledger is required by I4.

## Consequences

- afair can answer both "when was this true?" and "what did the system know at
  that time?" without rewriting history.
- Temporal inference can improve over time with full lineage and rollback by
  selecting an earlier span.
- No Neo4j, FalkorDB, Graphiti service, telemetry, or second source of truth is
  added.
- Existing vaults open additively; old edge rows remain readable.
- Every new edge gets a usable lower validity bound, initially conservative and
  later refinable.
- Graph traversal and MMR improve finding and diversity while keeping current
  FTS5, sqlite-vec, RRF, temporal relevance, provenance, and trust machinery.

## Alternatives considered

### Install `graphiti-core` and run a second graph database

Rejected. It duplicates storage and operational state, adds graph-server
dependencies, and makes disagreement between afair and Graphiti inevitable.

### Mutate `entity_edges.valid_from` / `valid_to`

Rejected by I2 and because it erases the history of what the system believed.

### Use relevance horizon as fact expiry for every event

Rejected. "This memory is no longer currently relevant" does not mean "the
fact became false". The automatic mapping is limited to one-off events.

### Add custom fixed entity schemas from Graphiti

Rejected. afair's emergent ontology and revisable kinds are stronger for I6.
Fixed Pydantic entity types would reintroduce a worldview the user must
maintain.

## Verification

- schema creation is idempotent and the new table rejects UPDATE/DELETE;
- explicit intervals normalize to UTC and reject inverted ranges before an
  edge insert;
- both cold-worker orderings converge on the same refined span;
- legacy edges backfill in bounded, idempotent batches;
- repeated worker runs deduplicate, while a bumped worker/operator version
  appends and becomes latest;
- world time and knowledge time are tested independently and together;
- malformed inferred dates cannot break edge creation;
- export contains the full validity ledger after its referenced entity edge;
- bounded graph traversal, hub suppression, MMR diversity, recall latency, and
  memory-quality regression are covered by the read-path verification.
- the live MCP schema advertises `as_of`; an actual historical recall returns
  `depth_used="deep"` and the normalized UTC lens in its note.

— [Actor: Codex · Generator: Codex GPT-5.6 Sol · 2026-08-03 15:08 Europe/Vienna · geändert]
