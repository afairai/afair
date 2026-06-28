# ADR-0001 — Why the constitutional invariants

> **Status:** Accepted
> **Date:** 2026-06-16
> **Audience:** anyone deciding whether a change is allowed, or whether the rules themselves still hold
> **Supersedes:** nothing. **Relates to:** [VISION.md §4](../../VISION.md) (the authoritative invariant text)

## Context

afair has eight invariants in VISION.md §4, declared inviolable: "If a future
evolution threatens an invariant, the evolution is wrong, not the invariant."
That is an unusually strong stance. Most projects keep their architectural
commitments as guidelines that bend under pressure. This ADR records **why
they exist, why they were drawn this way, and where their real risks are**, so
that a future contributor (or a future version of the author) can tell the
difference between a constraint that is load-bearing and one that has quietly
become wrong.

The short version: the invariants are not a wish-list. Each one is the
**negation of a specific, observed failure mode** in the memory / PKM / AI
space, and together they form a coherent system whose parts reinforce each
other. Making them constitutional trades adaptability for trust — the right
trade for a product whose entire value is "hand me your context for years."

## Decision

Keep the eight invariants as the irreducible kernel. The rationale, recorded
here, is part of what is being accepted — not just the rules but the reasons,
so the reasons can be re-examined without re-deriving them.

### Each invariant negates a concrete failure mode

| Invariant | The failure it refuses |
| --- | --- |
| **I1** MCP surface stability | API churn that silently breaks every integration built against you. A consumer that integrated at v1 must still work at v9. |
| **I2** Substrate immutability | Destructive migrations and in-place edits that lose history. "History is the truth"; the log is git-like. |
| **I3** Backward-compatible evolution | The standard escape hatch of "just rewrite the data." Migrations are forbidden; schema change is a new **view** over unchanged substrate. |
| **I4** User ownership by default | Lock-in, and export-as-a-premium-feature — the SaaS hostage model. Self-hosting and full local operation are baseline, not upsell. |
| **I5** Vendor neutrality | Single-provider risk (price, deprecation, ToS). The system must run on Claude, GPT, Gemini, Mistral, or local models. |
| **I6** Emergent over imposed | Rigid PKM schemas (notes/tasks/contacts straitjacket). No fixed ontology ships; categories emerge, merge, split, die from usage. |
| **I7** Recursive self-modification with rollback | Black-box drift in a self-improving system. Every change is recorded and reversible; I1–I6 are an exempt kernel it cannot optimize away. |
| **I8** Single-tenant by design | The entire class of cross-tenant data leaks — which cannot exist when nothing is shared. One machine per user. |

Each is a deliberate "not like everyone else," encoded as architecture rather
than left as a value statement.

### They are a system, not a list

The strength is not the individual rules but how they reinforce one another in
three chains:

- **I2 → I3 → I7.** An immutable, content-addressed log *enables* view-based
  evolution, which *enables* reversible self-modification. I7's "every change
  is reversible" is only credible because I2 guarantees there is an unaltered
  past to roll back to. Pull I2 and the other two collapse.
- **I4 ↔ I8.** "Yours, on your machine" and "one machine per user" are the
  same commitment seen from two sides. Together they are the moat:
  sovereignty plus isolation.
- **I5 ↔ I6.** Vendor neutrality and no-fixed-ontology are both
  flexibility-over-premature-commitment.

I7's exempt-kernel clause (I1–I6 cannot be self-modified) is the load-bearing
piece of constitutional engineering: the self-improving system may change its
*strategies* but never its own *guarantees*. That is the answer to "why won't
it drift away from what it promised."

### Clarification: I2 already sanctions user erasure

A common misreading (the author made it) is that a right-to-erasure feature
violates I2/I3. It does not. I2 reads: "Nothing is ever overwritten or deleted
**(except via explicit user-invoked right-to-erasure paths, which are
themselves logged)**." Erasure is an anticipated, sanctioned path. And I3 is a
*version*-compatibility guarantee (data written by an older version stays
readable by newer ones), not a promise that data is never deleted by its
owner.

So a per-item erasure feature is implementable **within** the invariants. The
open questions are *how*, not *whether*:

1. The erasure must be **logged** (I2's own clause requires it) — a tombstone
   event, append-only, recording that content at hash H was erased and why.
2. The bytes must actually become unrecoverable, including from backups and
   Litestream replication. The clean mechanism is **crypto-shredding**: a
   per-blob data key wrapped by the vault key, so erasure = destroy the
   wrapped key and the ciphertext is mathematically dead everywhere it was
   copied, with no need to chase every physical copy.

That I2 thought about erasure in advance is itself evidence the invariants
were designed, not reflexive.

## Consequences

### What we gain

- A contract strong enough to build a trust-based product on. Users (and
  integrating agents) can rely on guarantees that are expensive to revoke by
  construction.
- A coherent architecture that falls out of the invariants almost
  mechanically: an event log (write side) with rebuildable projections —
  interpretations, FTS, vector index — as the read side. This is CQRS in all
  but name, and it is *why* I3's "only views, never migrate" is sustainable.
- A foreclosed-by-design absence of whole bug classes: no cross-tenant leak
  (I8), no "which provider broke us" incident (I5), no "we shipped a migration
  that ate the data" (I2/I3).

### Accepted bets (the real future-proofing risks, named)

These are not flaws. They are deliberate bets where the cost lands later. They
are recorded so a future reader weighs them with eyes open.

1. **I8 unit economics — the biggest bet, and it is economic, not technical.**
   One machine per user is isolation-perfect but, at 10k users, is 10k
   machines. I8 + I4 structurally foreclose cheap multi-tenant SaaS economics.
   Fly scale-to-zero (`auto_stop_machines`) blunts the cost, but this is the
   invariant most likely to force a hard conversation at scale. We accept it:
   the wager is that isolation and sovereignty are worth more to this product
   than infra efficiency.

2. **I3 sustainability depends on projection discipline.** An append-only log
   that grows forever, with views over it, stays fast only while the
   projections (FTS, vector, interpretations) remain rebuildable from the
   substrate alone and are cached/materialized. A projection that needs
   external state to rebuild would silently break I3. The architecture already
   has the right shape; the risk is erosion over time, not a present defect.
   This requires ongoing vigilance, not a one-time check.

3. **I7 is aspirational today, not yet load-bearing.** The self-improvement
   tuner runs at `promote_enabled=False`: judge-evaluates-judge without a
   ground-truth eval set is research-grade dubious (see the comment in
   `mcp/server.py` and the recursive self-improvement design notes).
   The invariant promises more than the system currently delivers. That is
   acceptable for a north star, but it should be stated plainly: I7 today is a
   guardrail design, not lived behavior.

4. **I6 trades debuggability for flexibility.** "No fixed ontology" can shade
   into "no guarantees about what is in there." It needs strong introspection
   tooling (the parked Vault Dashboard) or the emergent structure becomes a
   black box. A softer risk than the three above, but real.

### The meta-tradeoff

Declaring the invariants *constitutional* is itself the core decision:
**adaptability traded for trust.** For a memory product, trust is the product,
so the trade is correct — but the cost is honest: if one invariant turns out
wrong, we have made it maximally expensive to change. The mitigation is that
they are few (eight), general, and mutually reinforcing, so the surface for
"one of them is wrong" is small.

## Alternatives considered

- **Guidelines instead of invariants.** Rejected: a memory vault whose
  guarantees bend under pressure is not a vault. The strength *is* the point.
- **Multi-tenant with row-level isolation (drop I8).** Rejected: it would
  fix the economics (see bet 1) but reintroduce the entire cross-tenant leak
  class and contradict the sovereignty brand (I4). The economics are a price
  we choose to pay, not a problem to engineer away by weakening isolation.
- **Allow destructive migrations behind review (soften I3).** Rejected: it is
  the exact escape hatch that erodes "old data stays readable forever." The
  CQRS/projection architecture makes the strict version affordable, so there
  is no need to soften it.
- **Pick a primary provider for performance (soften I5).** Rejected: provider
  diversity is increasing, not decreasing; the abstraction tax (litellm
  lowest-common-denominator) is the accepted cost of not being hostage to one
  vendor's pricing or roadmap.

## Re-examination triggers

Re-open this ADR if any of these become true:

- I8: per-user infra cost crosses the point where it threatens viability and
  `auto_stop` plus right-sizing no longer closes the gap.
- I3: recall latency degrades and the cause traces to view-over-log growth
  that projections cannot absorb.
- I7: a ground-truth eval set lands and the tuner can be promoted — at which
  point I7 stops being aspirational and its guarantees must be made real.
