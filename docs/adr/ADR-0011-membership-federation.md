# ADR-0011: Membership federation, one person across many sovereign vaults

> **Status:** Accepted
> **Accepted:** 2026-07-31 (after a 13-system prior-art review)
> **Date:** 2026-07-28
> **Audience:** anyone touching cross-vault credentials, the OAuth grant machinery, write routing, the provenance sidecar, sponsored-seat provisioning, or the fleet security model
> **Relates to:** VISION.md §4 (I1 additive surface, I4 user ownership, I8 single-tenant per principal), [ADR-0001](ADR-0001-constitutional-invariants.md) (the I8 economic bet this re-opens at enterprise scale), [ADR-0006](ADR-0006-event-provenance.md) (the provenance sidecar the `routed_by` stamp extends), [ADR-0010](ADR-0010-principal-generalization.md) (the principal model and the org-vault ground this builds on)

## Context

ADR-0010 made an organization a first-class principal: one org, one vault,
one machine, many writers. v0.1.27 then shipped org-direct connect, so a
member's AI client can hold a credential pointing straight at the org
vault. That covers the B2B v1 case: a team whose members each connect
their tools to the shared vault directly.

What it does not cover is the person. A person may belong to more than one
vault: their own personal vault, one or more shared org or team vaults,
and possibly a confidential circle (leadership, board, anything that must
stay dark from the rest of the organization). I8 says every one of those
vaults is its own single-tenant instance, so "membership in many vaults"
can never be one multi-tenant server. The question this ADR answers is
therefore topological: how does one person reach N sovereign single-tenant
vaults, given that hosted clients (claude.ai, mobile apps) connect to one
MCP endpoint per server and adding N connections per person per client per
device does not scale as a user experience?

Today's de facto answer is the worst one: the person copies N bearer
tokens into M AI-client configs on K devices, in plaintext, and every
recall or write targets exactly one vault at a time. That is N x M x K
plaintext credential copies and no cross-vault recall at all.

This ADR is the design of record for the differentiated cross-vault layer
on top of org-direct connect. The decisions below were operator-confirmed
on 2026-07-28 after an adversarial re-check. Build is deferred to v2, post
personal-launch; nothing in this ADR ships now.

## Decision

Five decisions of record: the gateway topology (F1), credential-pinned
write routing (F3), the two-tier confidentiality model, the billing-only
sponsored seat (F5), and the machine-per-vault plus invite-first grant
model (F2/F4).

### 1. F1: the personal vault is the gateway

Each person's own single-tenant instance is the gateway to every vault
they belong to. The personal vault holds the person's membership
credentials toward the org and team vaults, fans recall out across them,
and routes writes. The person's AI clients connect once, to the one MCP
endpoint they already have; multi-vault membership is a property of that
endpoint, not of the client configuration.

Four conditions are part of the decision, not implementation detail:

- **Membership credentials are OAuth 2.1 rotating refresh tokens.** The
  gateway participates in the existing grant machinery (single-use
  rotation, reuse detection, confidential-client verification, v0.1.16),
  never the non-expiring `api_tokens` path. A gateway credential that
  leaks stops working at the next rotation; a leaked static token works
  forever.
- **Direct connect always remains available.** The gateway is a
  convenience path, never the only authorized path. Every vault, personal
  or shared, stays directly reachable by its authorized members with their
  own credentials (I4: the principal owns each substrate and can always
  reach it without an intermediary). Losing the gateway degrades comfort,
  never access.
- **Recall fan-out is fault-isolated per member vault.** One member vault
  being down or slow must not fail the whole recall. The gateway serves
  what it got, flags the response as partial, and names which vault did
  not answer. Graceful degradation is a contract, not an aspiration.
- **The fleet-compromise amplification risk is documented.** A gateway
  that can reach N vaults is a more valuable target than a vault that can
  reach one. This risk belongs in SECURITY.md when the feature builds
  (the SECURITY.md update happens with the build, not with this ADR).

Stated plainly: this design is a net reduction in credential blast
radius versus the status quo. Today the same tokens sit in plaintext in
every AI client's config on every device (N vaults x M clients x K
devices, each copy a full-privilege bearer credential). The gateway
centralizes membership credentials into one SQLCipher-encrypted store
inside the person's own instance, rotating instead of static. The
amplification risk is real and is documented; the plaintext-sprawl risk
it replaces is larger.

### 2. F3: default write target, credential-pinned

The honest name for this decision is: private-by-default plus opt-in
shared defaults, per credential.

A write's default destination is carried by the credential, fixed at mint
or grant time, never by a per-request or per-project hint the model
supplies. Model-supplied routing hints are rejected here explicitly as a
memory-laundering vector: an untrusted model that can choose the
destination of a write can redirect a confidential fact into a shared
vault, and no storage-layer control can undo that after the fact. The
default target is the person's private vault unless the operator
explicitly configures a specific credential to default to a specific
shared vault.

Pinning the default to the credential is deliberately stronger than the
incumbent pattern. Password managers are private-by-default too, but
their default is a mutable client-side setting, and that mechanism is
where they fail: 1Password's default vault resets to the private vault
on a new device or browser-extension install (save-to-wrong-vault is
its most-reported save UX defect), and Bitwarden organization items not
assigned to any collection silently disappear from the user's own vault
view until an admin recovers them through an "Unassigned" filter.
Private-by-default is necessary but not sufficient; the mechanism that
holds the default is what matters, and a credential-pinned default
cannot drift per install, device, or request.

Four conditions are part of the decision:

- **Only the operator configures a credential's default target.** Never
  the model, never another member. Configuration is an operator act on the
  credential, in the same trust position as `recall(decide=)`.
- **Every default-routed write is stamped.** A write that landed in a
  vault because the credential's default sent it there carries
  `routed_by=default` in the ADR-0006 provenance sidecar. The sidecar
  extends naturally: routing is server-derived transport fact, out of the
  content hash, exactly where ADR-0006 put credential identity.
- **Supersession of a mis-routed write propagates immediately.** The
  correction is a new event marking the prior one superseded, append-only
  (I2), never a delete. A mis-route is fixed the way every wrong fact in
  afair is fixed: by a later event, visible in history.
- **The residual hazard is named.** A person working in a shared-default
  context who dictates something confidential lands it in the shared
  vault. Mirror surfacing detects this after the fact; it does not contain
  it. Other members' AI sessions may already have read the fact before the
  supersession lands. Detection is not containment, and this ADR does not
  pretend otherwise. The structural answer to that hazard is the next
  decision.

The residual hazard splits into two laundering kinds, and F3 closes only
the first. Destination-laundering is closed: the model cannot choose
where a write goes. Content-laundering is not: the model still composes
what bytes go into a write to a legitimately pinned shared vault. The
adversarial shape is EchoLeak (CVE-2025-32711): privileged content (a
personal-vault recall) plus untrusted content (a poisoned shared-vault
memory) plus a compliant write path out.

The v2 build carries a named mitigation for content-laundering,
graduated by signal strength. This is a design commitment for the v2
build, not something shipping now:

- The gateway compares an outgoing shared-vault write against the
  personal-vault content recalled in the same session, using the
  existing entity-overlap and embedding machinery.
- On strong overlap, the write is held on the writer's own gateway,
  recorded append-only there as the audit trail, and not forwarded to
  the shared vault. It surfaces in the Memory Mirror for the writer,
  who is the data subject, to publish or keep private; other members
  never see it before that decision. This is containment, not just
  detection.
- On weak or no overlap, the write proceeds and is listed in a Memory
  Mirror "recent writes to shared vaults" view so the owner can see it.
  This also closes the mis-route-visibility gap left by condition c: a
  mis-routed write is only fixable by supersession if the owner sees
  it.
- With no personal recall in the session, no action is taken.

The boundary is stated so the mechanism is not false comfort: this
makes Tier 1 defense-in-depth, not leak-proof. A model that rewords
personal content can duck the overlap check. The structural guarantee
remains Tier 2, where the confidential vault is not on the gateway at
all; anything that truly must not leak belongs in Tier 2. On the
invariants: the write is always recorded append-only (I2), the hold is
a serving-and-forwarding overlay and never a substrate mutation, the
frozen verbs are unchanged (I1), and the owner stays in control (I4).

### 3. The two-tier confidentiality model

Once one AI session has read two vaults, nothing at the storage layer can
prevent content from the first appearing in a summary written to the
second: the laundering happens in the context window, outside afair's
reach. The only control that works is structural: make sure the session
never held both.

- **Tier 1 (convenience):** the personal vault and shared team vaults ride
  the F1 gateway on one connection. Cross-pollination between these vaults
  through the model's context is an accepted cost of the convenience; the
  membership boundary here is organizational, not adversarial.
- **Tier 2 (confidential):** a confidential circle is direct-connect only.
  The gateway never holds its membership credential, so a gateway session
  structurally cannot read it: containment is by construction (the token
  is not there), not by asking a human to remember to open the right
  session. Leadership, board, and anything that must stay dark from the
  rest of the org lives in Tier 2.

Structural containment is not paranoia; it is the emerging security
consensus. Microsoft Research argues that once multiple participants
contribute to one shared context, document-level and user-level ACLs
are insufficient and access control must be participant-aware inside
processing ("Enterprise AI Must Enforce Participant-Aware Access
Control", arXiv 2509.14608, 2025). The OWASP Agentic Security
Initiative Top 10 names the failure class as ASI06, Memory and Context
Poisoning. Matrix/Megolm shows the empirical shape: one client
aggregates many independently encrypted rooms, so device-compromise
blast radius grows with the number of aggregated stores and the
aggregation layer itself is the attack surface (CVE-2022-39250). No
prior-art system we examined offers a first-class "this store is
deliberately unreachable from the aggregating client" primitive; Tier 2
is that primitive, the guarantee a pure per-key aggregator like Matrix
structurally cannot make.

The cost is stated honestly: Tier 2 reintroduces exactly the separate
per-vault connection the gateway exists to remove. That is the deliberate
price of real isolation, and it is paid only for the circles that need
it.

A vault's tier is not fixed at creation, because real confidentiality
needs emerge after a vault exists: a team channel becomes a leadership
channel. Moving from the convenience tier to the confidential tier is a
cheap one-way door: revoke the gateway's membership credential for that
vault and it is off the aggregator immediately. Moving the other way,
confidential to convenience, widens exposure and therefore requires
deliberate operator ceremony. The trap avoided is a creation-time-only
choice, which would force re-creating the vault to change its tier;
that is the shape of ChatGPT's project-only-memory setting, settable
only at project creation and never after.

### 4. F5: sponsored seat, billing-only

An organization may pay for a member's personal vault, so that every
member has a gateway anchor and a departing employee keeps their own
memory. Sponsorship is billing-only, specified as:

- The org holds no encryption key, no credential, and no administrative
  authority over a sponsored personal vault. It cannot read it, cannot
  reach it, cannot reset it.
- On sponsorship lapse the vault is suspended and exported to the person,
  never deleted.
- The employer is never the data controller of a personal vault; the
  person is.

This is why sponsorship does not contradict I4: ownership of the
substrate stays with the person in every technical and legal sense; the
org's relationship to the vault is a line item on an invoice. Provisioning
is lazy, on the member's first use, not eager at grant time; unclaimed
seats cost nothing and create no empty vaults.

The market has validated this exact line: 1Password Business includes
a free 1Password Families membership on a strict
subscription-versus-ownership boundary. The business shares only
subscription status, never ownership or access rights; it cannot
access or manage the family account; only the family organizer can
delete it; and on leaving the job the person keeps all data (billing
transfers, data does not move). F5 adopts the same line: sponsor pays
the bill, principal owns the vault, sponsor has zero read, manage, or
delete authority, and offboarding transfers billing, not data.

One structural difference from the password-manager analogue is named,
because it makes the data-controller question live rather than
theoretical. The sponsored personal vault is the F1 gateway, so it
accumulates work-derived traces by construction: recall traces,
observations from work sessions. A purely personal family vault into
which no work content flows has no such accumulation. This ADR
therefore refuses employer reach on the record: no organization DLP
scanning, no legal hold, and no e-discovery reach into a sponsored
personal vault. The person is the sole data controller; the
organization is payer only. If a jurisdiction compels otherwise, that
is the regulatory re-examination trigger already registered below, not
a silent capitulation.

One economic constraint is recorded without numbers: because each vault is
its own machine (I8), a sponsored seat carries an always-on per-seat
infrastructure floor. A sponsored-seat product must price above that
floor or be a deliberate, bounded loss leader. Specific figures are a
business decision outside this public ADR.

### 5. F2 and F4: machine per vault, invite-first grants

**F2** restates I8 for the org side: each vault, personal, team, or
confidential circle, is its own single-tenant instance. Org-side machine
count is driven by the number of circles the org actually needs, not by
seat count. An org with one shared vault and forty members runs one org
machine, not forty.

**F4** fixes the v1 grant model: individual invites, plus batch invites
for onboarding many members at once. Teams as a first-class group with
cascading grants (add a person to the group, they gain every vault the
group has) is a fast-follow, with one binding requirement: cascading
revocation is designed in the same change as cascading grants. A grant
primitive without a matching revoke primitive is an incomplete design,
because the moment that matters most (offboarding) is the revoke path.

## Invariant integrity

- **I1 (surface stability).** The three frozen verbs do not change.
  Membership is a deployment and credential-topology concern, not a
  wire-surface concern: the gateway speaks the same remember, recall, and
  observe to its clients and to its member vaults. If the surface ever
  needs to grow (for example, a partial-results flag added to recall
  output, or an optional routing argument), the change must be additive
  per I1 and pass the golden surface guard, exactly as ADR-0006 added the
  `client` output field without touching any input; v1 of this design
  needs neither.
- **I4 (user ownership).** Every vault stays directly reachable and owned
  by its principal. The gateway is never a required intermediary (F1
  condition b), and the sponsored seat transfers no key, credential, or
  control to the sponsor (F5). Export and self-hosting are untouched.
- **I8 (single-tenant).** Preserved, not bent. The gateway is a
  client-side fan-out running inside one principal's own instance over
  many single-tenant instances; it is not a multi-tenant server, holds no
  other principal's substrate, and shares no database or application
  server with anyone. F2 restates the invariant for org topology.
- **The ADR-0001 economic bet.** I8's accepted bet is that per-principal
  machines stay economically viable. Enterprise scale (hundreds of circles
  per org, thousands of sponsored seats) stresses that bet harder than
  consumer scale does, and this ADR registers itself as an ADR-0001
  re-examination trigger for exactly that case. The sponsored-seat floor
  in F5 is the same bet seen from the pricing side.

I2, I3, I5, I6, and I7 are not materially engaged by this design: no
substrate semantics change, no migration occurs, no provider is
privileged, no ontology ships, and no self-modification surface is
touched. The `routed_by=default` stamp follows the ADR-0006 sidecar
discipline (append-only, out of hash) and inherits its I2/I3 posture.

## Consequences

**What gets better.**

- One connection per person per client reaches every Tier 1 vault they
  belong to; cross-vault recall exists for the first time.
- Credential sprawl collapses: N x M x K plaintext token copies become
  one encrypted, rotating credential store per person.
- A departing employee keeps their personal vault (F5), and an org's
  confidential material has a structural home (Tier 2) instead of a
  policy-only one.
- Write routing is deterministic and auditable: the credential decides,
  the sidecar records, and a wrong default is visible as
  `routed_by=default` in provenance.

**What it costs.**

- Tier 2 circles pay the separate-connection price. That is the point,
  but it is friction, and it must be explained honestly in product copy.
- The per-seat infrastructure floor (I8 economics) bounds how cheap a
  sponsored seat can ever be.
- Cascading grants carry a design obligation: no group-grant primitive
  ships without its revoke twin (F4).
- The gateway concentrates risk: a compromised gateway instance reaches
  every Tier 1 vault its owner belongs to. This is a net improvement over
  plaintext sprawl (F1) but it is a new, named item for SECURITY.md at
  build time.
- The shared-default dictation hazard (F3 condition d) is detected by the
  Mirror but not contained by anything. Tier 2 exists because this class
  of hazard cannot be contained inside Tier 1.

## Alternatives considered and rejected

- **A shared thin router (one hosted routing service fanning out for all
  users).** Rejected on I8: a shared component holding many principals'
  membership credentials and touching many principals' traffic is exactly
  the shared application server the invariant forbids, whatever the
  marketing name.
- **A local proxy on the person's device.** Works for CLI clients, and a
  self-hoster can still build one. Rejected as the product answer because
  it does not reach hosted clients: claude.ai and mobile apps connect to
  a public MCP endpoint, not to a process on the user's laptop.
- **Model-supplied routing hints (per-request or per-project write
  targets chosen by the model).** Rejected as a memory-laundering vector
  (F3): the destination of a write is a trust decision, and the model is
  not in the trust position to make it.
- **Strict private-by-default with no shared defaults at all.** Every
  shared-vault write would need explicit per-write operator action.
  Rejected as fatal B2B friction: a team vault nobody can conveniently
  write to is a team vault nobody writes to. The credential-pinned
  default (F3) keeps the safety property (the model never chooses) while
  making shared vaults usable.
- **Sponsored vault with org-held keys or org admin.** Rejected as an I4
  violation: a personal vault the sponsor can read or control is not the
  person's vault, whoever pays for it. Billing-only (F5) is the only
  sponsorship shape compatible with the constitution.

## Prior art and security considerations

- **OWASP ASI06 maps onto the existing posture.** Its three
  recommendations, segment memory per tenant, expire unverified data,
  and track provenance, correspond to controls already in force:
  single-tenant per principal (I8), the append-only substrate (I2), the
  provenance sidecar (ADR-0006), and operator confirmation of derived
  beliefs (ADR-0002). afair already does what the guidance recommends;
  this ADR adds topology on top.
- **The gateway's configuration surface must be role-gated, not merely
  authenticated.** CVE-2026-49948 documents a memory system whose
  self-hosted configuration endpoint checked authentication but not
  role, letting any authenticated user repoint model and embedder
  configuration for everyone on the instance. F3 condition a ("only the
  operator configures a credential's default target") therefore carries
  a build obligation: the default-target surface enforces the operator
  role and rejects an authenticated non-operator member, exactly as at
  `recall(decide=)`.
- **The threat is real but not yet realized.** There is no publicly
  documented incident of one consumer's stored memories being served to
  a different user; the documented memory failures are self-poisoning
  and enterprise cross-scope leakage. Two-tier containment is
  precautionary against a real but not-yet-realized class of risk, and
  this ADR does not overstate the threat.

## Re-examination triggers

- **Enterprise scale.** If org adoption reaches a scale where per-circle
  machines and per-seat floors dominate the cost structure, the ADR-0001
  I8 economic bet is formally re-examined. This ADR is a registered
  trigger for that re-examination, not a pre-authorization to weaken I8.
- **Hosted-client protocol evolution.** If hosted MCP clients gain clean
  first-class support for multiple concurrent server connections, the
  gateway's necessity shrinks (direct connect per vault becomes viable
  UX), and F1's topology should be revisited before more is built on it.
- **Regulatory data-controller questions.** If a regulator or a
  jurisdiction treats a sponsoring org as controller of a sponsored
  personal vault despite the billing-only design, F5's legal framing must
  be re-examined with counsel before the sponsored-seat product ships or
  continues in that jurisdiction.

## Build status

Build is deferred to v2, after the personal launch. This ADR is the
design of record: the decisions above are operator-confirmed and bind
future implementation, but no code, schema, credential machinery, or
SECURITY.md change ships with this document. Registry and cross-reference
updates (CLAUDE.md, SECURITY.md, ADR-0001 trigger pointer) land with the
build or with acceptance of this ADR, whichever comes first.
