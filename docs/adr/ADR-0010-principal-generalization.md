# ADR-0010: Principal generalization

> **Status:** Accepted
> **Date:** 2026-07-28
> **Audience:** anyone touching the constitutional I8 text, the principal settings, the prompt and surface framing helpers, or the `actor` field on the write path
> **Relates to:** VISION.md §4 (I8, the amended text), [ADR-0001](ADR-0001-constitutional-invariants.md) (the invariant rationale this deliberately re-examines), [ADR-0002](ADR-0002-belief-revision-derived-layer.md) (advisory fields never raise trust), [ADR-0006](ADR-0006-event-provenance.md) (the client provenance sidecar the `actor` axis complements)

## Context

afair was written assuming the vault belongs to one person. The assumption
lives in three layers. The constitutional text says "exactly one user" (I8,
VISION.md §4) and "one isolated instance per user" (the economics passage and
the durable boundaries). The cognition prompts speak of "a personal memory
vault" and "the user's substrate": the extractor, the consolidator, the
conflict resolver, the entity canonicalizer, the entity deduplicator, the
schema evolver, and the synthesis workers all frame their work around a single
person. The MCP surface prose (server instructions, tool descriptions, the
session-start resource) does the same.

An organization can want exactly what a person gets: one vault, one instance,
one database, one machine, one legal entity that owns the substrate. That is
one tenant. It is not multi-tenancy. What changes inside the instance is that
several writers, the organization's members and its agents, write into the
same vault. Multiple writers within one tenant is an attribution question,
not an isolation question. The failure mode I8 negates (cross-tenant data
leaks, which cannot exist when nothing is shared) survives untouched, because
nothing is shared between principals.

What stays forbidden does not move: two organizations in one instance, an
organization and an unrelated person in one instance, any shared database or
application server between principals. One instance never serves two
principals.

Rewording constitutional text requires an ADR. This one engages ADR-0001
deliberately. ADR-0001's re-examination triggers for I8 are economic; this is
a semantic re-examination instead: it asks who the "one" in single-tenant is,
not whether single-tenant holds. The economic bet is untouched. If anything,
organization principals improve it, since one dedicated machine now serves a
paying organization rather than a single consumer subscription.

One piece of ground is already built, and it matters for scope. ADR-0006
shipped per-credential provenance: every HTTP write stamps a
credential-derived client slug into the append-only `event_provenance`
sidecar, and API token labels become that slug. A team where each member
holds their own token therefore needs zero new code for attribution. Member
tokens are the default and recommended path for organizations in v1. The
only gap is the shared-credential case: one organization-level agent writing
on behalf of many members through a single credential. From the credential
alone the vault cannot tell whose statement it is recording. That gap is
what the `actor` field closes.

## Decision

The generalization ships in five parts: a constitutional amendment, two
settings, prompt and surface framing, the `actor` field, and an explicit v1
access model.

### 1. Constitutional amendment: user becomes principal

VISION.md §4 I8 is amended to read:

> ### I8. Single-Tenant by Design
> Every deployed instance, self-hosted or managed, belongs to exactly one
> principal: a person or a single organization. No shared database, no shared
> application server, no row-level principal separation. The hosted offering
> provisions a dedicated machine per paying principal. Multi-tenancy is
> forbidden architecturally, not just practically: one instance never serves
> two principals. An organization's instance is one tenant with many writers,
> its members and agents, which is an attribution concern, never an isolation
> boundary. The orchestration layer that manages billing and provisioning may
> be shared; principal data and application state never are.

The companion spots change with it: the VISION.md §2 single-tenant principle,
the §3 economics passage, the §11 durable boundary ("one isolated instance
per principal"), the CLAUDE.md §5 I8 summary, and the ADR-0001 failure-mode
row and economics bet, with a one-line pointer in ADR-0001 recording that the
wording was generalized here while the negated failure mode and the economic
bet are unchanged.

### 2. Principal settings

Two settings describe the principal, and they are the entire configuration
surface of this ADR:

- `principal_kind`: `"person"` or `"organization"`, defaulting to
  `"person"`.
- `principal_name`: required if and only if the kind is `organization`.
  The value is sanitized (stripped, control characters and newlines
  collapsed, length capped) because it is interpolated into system prompts.

A person vault that sets a name has the name ignored in v1, so person framing
stays byte-identical to the pre-change strings. For the hosted fleet, the
interface is these two environment variables per instance and nothing else;
provisioning logic lives outside this repo.

### 3. Prompt and surface framing

A single framing helper renders the person or organization variant of every
enumerated framing site: the extractor tool description and system prompt,
the consolidator, the conflict resolver, the entity canonicalizer, the
entity deduplicator, the living synthesis worker, the schema evolver, the
legacy entity-article writer, and the MCP surface prose (server
instructions, tool descriptions, session-start resource). An organization
vault gets organization-framed cognition ("this organization's substrate",
organization-appropriate voice rules); a person vault renders exactly the
strings that shipped before this ADR.

Two contracts hold this in place:

- **Byte identity for person vaults.** The person rendering of every framing
  site is snapshot-tested against a frozen copy of the pre-change string.
  The product is in daily use by person vaults; nothing about their
  cognition may shift as a side effect of this generalization.
- **Prose only, never schema.** Framing changes descriptions and
  instructions, never tool schemas. The golden surface guard keeps the wire
  contract fixed; an organization vault can never fork the wire contract.

One deliberate exemption: the tuner's judge quality criteria stay frozen in
their current wording, alongside the versioned judge prompt, so past
evaluation scores remain reproducible. Engineering prose (code comments,
docstrings) that says "user" in the generic sense is left alone.

### 4. The `actor` field

`remember` and `observe` gain an optional advisory `actor` field: an
identifier for the person or agent on whose behalf a write is made, for the
case where one credential is shared. An organization agent relaying a
member's statement passes, for example, `actor="slack:U123"` or a member
name; the vault records whose statement it is even though the credential is
the agent's.

Placement follows the boundary ADR-0006 drew: caller-supplied information is
content, credential-derived information is sidecar. `actor` is
caller-supplied, so it lives in the event payload and inside the content
hash, exactly like `asserted_by`. In-hash is semantically correct here: the
same sentence written on behalf of member A and on behalf of member B is two
different assertions, and a dedup that collapsed them would merge two
people's facts.

That completes a four-axis provenance model:

| Axis | Question it answers | Where it lives | Who sets it |
| --- | --- | --- | --- |
| `origin` | Which intake path wrote this (remember, observe, import) | In the payload, inside the content hash | The server; coarse by design |
| `client` | Which credential wrote this | The `event_provenance` sidecar, outside the hash (ADR-0006) | The server, derived from the credential |
| `asserted_by` | Did a human or a model assert this | In the payload, inside the content hash | The caller; advisory |
| `actor` | On whose behalf, when the credential is shared | In the payload, inside the content hash | The caller; advisory |

Handling rules: whitespace and control characters are stripped; a value that
is blank after stripping is treated as absent; long values are truncated
with the full value preserved in a companion payload field (the same
truncate-preserve convention the intake path already uses). The value is
never slugged: an identifier like `slack:U123` must survive verbatim,
because it is content with meaning, not a credential to normalize. Recall
serves both axes: `client` answers which tool held the pen, `actor` answers
whose statement it is. `actor` is advisory in the ADR-0002 sense: it never
raises trust, and it never substitutes for `client`. When `actor` is absent,
`client` is the best available attribution, and the tool descriptions say
so.

### 5. v1 access model: attribution, not isolation

Every credential a principal issues sees the whole vault. An organization's
member token reads everything the organization's vault holds. Access
control, namespaces, and partial visibility are explicitly out of scope:
they would need their own ADR, because partial visibility tangles with the
append-only substrate (I2) and with recall honesty (what a viewer-relative
recall is allowed to claim it does not know). v1 answers "who wrote this"
and deliberately does not answer "who may read this" beyond
whole-vault-or-nothing.

### Out of scope in v1

- Per-token default actor set at mint time. A token-derived default is
  credential-derived state and belongs in the sidecar, where the client slug
  already carries it. Revisit if shared-agent usage demands it.
- `by_actor` statistics. `actor` is payload and can be scanned; a dedicated
  projection can be added later under I3 without touching stored events.
- New verbs, recall signature changes, migrations, schema changes, or
  entity-model changes. None are needed.
- Hosted provisioning changes in this repo. The two environment variables
  are the whole interface.

## Consequences

**A person vault upgrading across this change experiences zero change.**
This is the primary regression guard, enforced three ways: byte-identity
snapshots for every framing site, an unchanged golden MCP surface at person
defaults, and content-hash regression tests proving that writes without
`actor` hash exactly as they did before. Nothing merges without all three.

**An organization vault gets organization-framed cognition over unchanged
mechanics.** Same substrate, same workers, same wire contract; only the
prose the models and clients read changes.

**The dedup shift for actor-bearing writes is deliberate.** The same content
with different actors produces two events. Writes without `actor` are
byte-identical to today's hashing, so existing vaults and existing dedup
behavior are untouched.

**Conflict-resolver interaction.** Actor-fragmented duplicates (the same
text written on behalf of different members) are semantically near-identical
event pairs, and the conflict resolver will surface them as candidate pairs.
The resolver prompt classifies verbatim-identical statements as
confirmations or as unrelated, never as conflicts, so these pairs do not
become conflict flags. The honest cost remains: redundant cold-path work
(candidate screening, consolidation passes) proportional to relay fan-out.
That cost is accepted as the price of honest attribution, and it is handled
downstream by the consolidator and the dedup layers, never by suppressing
the writes at intake.

**Relay fan-out stance.** When an organization agent relays the same fact on
behalf of N members, the vault records N attributed assertions, not one
deduplicated event. Recall's compact view and the consolidator absorb the
repetition on the read side. Write-time never suppresses on `actor`: a
collapse would silently drop the attribution of every member after the
first, which contradicts the reason `actor` exists.

**Injection-safety contract.** `actor` reaches the extractor because the
user-message builder forwards extra payload fields into the extraction
prompt, and this is wanted (the extractor can link the actor to entities).
Its injection safety derives from one property: the builder emits it inside
the untrusted fence (`wrap_untrusted` in the prompt builder), where the
model is instructed to treat content as data, not instructions. The length
cap is a size bound, not the safety mechanism. This is a maintenance
contract: any future change that surfaces `actor` outside the untrusted
fence re-opens the prompt-injection surface and must be rejected in review.

**Hosted operations.** Provisioning an organization instance means setting
two environment variables. Everything else on the fleet is identical to a
person instance.

## Alternatives considered

- **Multi-tenancy proper (many principals, one instance, row-level
  separation).** Rejected, and already rejected by ADR-0001: it reintroduces
  the entire cross-tenant leak class and contradicts the sovereignty
  commitment (I4). The point of this ADR is that organizations do not need
  it: an organization is one tenant.
- **A separate organization edition or fork.** Rejected: it would duplicate
  the whole system to change pronouns in prompts and prose. Two codebases
  would drift, and every substrate improvement would need double shipping.
- **Organization-as-person without an ADR.** An organization could run a
  person-worded vault today and look away from the text. Rejected: off-label
  use of the constitution is corrosive. The first time the constitutional
  text and the deployed reality disagree, the text stops binding, and the
  invariants are only worth what the text binds.
- **Write-time collapse of actor-bearing duplicates.** Rejected for the
  reason stated in Consequences: collapsing on content while ignoring
  `actor` silently discards attribution, which is the field's purpose.
- **Per-token default actor at mint time (now).** Deferred, not rejected:
  the sidecar already carries credential identity, and a server-injected
  default inside the payload would put server state into the content hash.

## Invariant fit

- **I1 (surface stability).** `actor` is a trailing optional parameter on
  `remember` and a new optional field on the observe event model. The golden
  surface diff is additive: new property, extended descriptions, no
  removals, no tightening.
- **I2 (substrate immutability).** Untouched. `actor` is payload inside
  ordinary append-only events; no new substrate tables are introduced (the
  provenance sidecar predates this ADR).
- **I3 (backward compatibility).** No migration. Old events simply lack
  `actor`; absence stays readable and meaningful, with `client` as the
  attribution.
- **I4 (user ownership).** Unchanged in substance: an organization owns its
  substrate exactly as a person does, and export carries everything,
  provenance included.
- **I5 (vendor neutrality).** Framing is provider-neutral prose; no provider
  is privileged by any part of this design.
- **I6 (emergent over imposed).** No new ontology ships. Organizations,
  members, and agents already emerge as entities from usage; `actor` gives
  the extractor one more honest signal to ground them.
- **I7 (recorded, reversible self-modification).** Prompt framing revisions
  are committed, versioned, and reversible; the person byte-identity
  snapshots are the rollback proof.
- **I8 (single-tenant).** Reworded, not weakened. The single tenant is now a
  principal, a person or a single organization; the negated failure mode is
  intact, and ADR-0001 carries the pointer.
