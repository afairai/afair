# ADR-0007: Emergent living syntheses

> **Status:** Accepted
> **Date:** 2026-07-14
> **Audience:** anyone touching clustering, synthesis, recall ordering, provenance, or cold-path scheduling
> **Relates to:** VISION.md §4 (I2, I3, I6, I7), §5.5 (emergent ontology), §6.5 (cold path), [ADR-0002](ADR-0002-belief-revision-derived-layer.md) (belief revision), [ADR-0003](ADR-0003-emergent-ontology.md) (revisable kinds)

## Context

The first topic-axis abstraction in afair was one article per canonical
entity. That proved the value of materializing a current synthesis before a
query. It also imposed the wrong organizing rule. A person's useful memory is
not always about a named entity. A decision may span people and projects. A
recurring concern may have no stable noun. One broad entity such as the user or
their company can connect most of the vault without making those records one
topic.

Asking the user to create topics would move the organization burden back to the
person. Asking an LLM to choose arbitrary groups in one pass would make the
result unstable, expensive, and difficult to test. A fixed enum of project,
person, company, task, and topic would violate I6.

## Decision

afair discovers evidence clusters automatically, then asks a language model to
name and synthesize each discovered cluster. Discovery is deterministic. The
model cannot choose a category or add evidence.

The semantic structure is revisable. The trust structure is fixed.

### Fixed trust structure

Every living synthesis must:

1. point to immutable source events through `parent_hashes` and `citations`;
2. resolve each model-produced key point to the numbered source records that
   support it;
3. preserve open questions instead of filling gaps;
4. record discovery signals and evidence strength;
5. keep prior versions through append-only invalidation;
6. remain regenerable from the substrate.

These are constitutional constraints, not topic categories.

### Emergent semantic structure

Candidate discovery combines three existing signals:

1. **Entity recurrence.** At least three live source events mention the same
   canonical entity. Mature vault-wide hub entities are excluded.
2. **Semantic proximity.** Binder links connect events when a one-way link is
   very strong, or when two links are reciprocal at a wider threshold.
3. **Explicit lineage.** Parent and child events, and siblings that share a
   parent, form a candidate when at least three live events are connected.

Candidates with substantial evidence overlap merge before synthesis. This lets
independent signals reinforce the same body of evidence without producing
duplicate articles.

There is no category field in the stored payload. The model writes a specific
title that may change on the next synthesis. Names are presentation, not
identity.

### Identity and lineage

A new cluster receives `cluster:<sha256(initial member hashes)>`. Later
candidates match a live prior cluster by evidence overlap, with a smaller
entity-overlap contribution. The best continuation keeps the existing cluster
ID.

When one prior cluster splits, the strongest child keeps the ID. Other children
receive new IDs and record the prior ID in `ancestor_cluster_ids`. When several
prior clusters merge, the strongest ID continues and every matched prior is
recorded as an ancestor. `previous_synthesis_hashes` links the actual versions.

Cluster identity therefore survives ordinary growth without pretending that a
split or merge never happened.

### Update and serving behavior

An unchanged evidence set is a no-op and makes no model call. New evidence
writes a new `living_synthesis` event and invalidates the prior live synthesis.
The old event remains in the substrate. Its regenerable FTS row is removed so a
stale version cannot lead current recall.

Recall presents a matching living synthesis before legacy entity articles and
raw events. The underlying rank order stays stable inside each group. Legacy
`entity_article` events remain readable forever under I3, but the scheduler no
longer creates new ones.

### Bounds

The worker is intentionally bounded:

- newest 400 live source events considered per cycle;
- at least 3 events per cluster;
- at most 40 source events per synthesis;
- at most 6 model calls per cycle;
- one run every 6 hours;
- hub suppression starts only at 12 mentions and 45 percent of the eligible
  vault window;
- one-way semantic distance must be at most 0.18;
- reciprocal semantic distance must be at most 0.32;
- candidate merge Jaccard threshold is 0.65;
- prior continuation score is at least 0.25.

The constants are implementation policy, not ontology. They can be evaluated
and tuned without changing the MCP surface or substrate history.

## Consequences

- The user never creates a cluster, folder, tag, page, or summary.
- Useful structure can form around unnamed themes as well as entities.
- A broad person or company does not automatically turn the whole vault into
  one topic.
- The model writes language after evidence selection, which limits its blast
  radius and makes discovery testable without model calls.
- Syntheses can rename, split, merge, fade, and return as evidence changes.
- Every current statement remains traceable to source events.
- The new event kind is additive. The frozen three-tool MCP surface does not
  change.

## Alternatives considered

### Keep one article per entity

Rejected as the primary structure. It misses unnamed themes and creates broad
hub articles. The implementation remains as a legacy reader and compatibility
path.

### Let the language model cluster the full vault

Rejected. The result is difficult to reproduce, costly to rerun, exposed to
prompt injection across a large input, and impossible to evaluate separately
from prose quality.

### Ship fixed categories

Rejected by I6 and by the product promise. The user should not inherit a
generic worldview from the software.

### Ask the user to confirm every cluster

Rejected as a default because it turns organization into an inbox. Corrections
remain available, but ordinary maintenance is automatic.

## Verification

The test suite locks the following behavior:

- entity, semantic, and lineage signals each form automatic candidates;
- weak semantic links do not form a candidate;
- invalidated evidence is excluded;
- mature hubs are suppressed;
- an unchanged cluster makes no model call;
- new evidence keeps cluster identity and supersedes the prior version;
- split lineage is explicit;
- every stored citation references a real selected source;
- no stored category field exists;
- recall prefers living syntheses while preserving legacy readability.

