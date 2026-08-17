# ADR-0011: Membership federation, one person across many sovereign vaults

> **Status:** Accepted
> **Accepted:** 2026-07-31 (after a 13-system prior-art review)
> **Date:** 2026-07-28
> **Audience:** anyone touching cross-vault credentials, the OAuth grant machinery, write routing, the provenance sidecar, sponsored-seat provisioning, or the fleet security model
> **Relates to:** VISION.md §4 (I1 additive surface, I4 user ownership, I8 single-tenant per principal), [ADR-0001](ADR-0001-constitutional-invariants.md) (the I8 economic bet this re-opens at enterprise scale), [ADR-0006](ADR-0006-event-provenance.md) (the provenance sidecar the `routed_by` stamp extends), [ADR-0010](ADR-0010-principal-generalization.md) (the principal model and the org-vault ground this builds on)
> **Amendment (2026-08-17):** a second adversarial design review raised 13
> findings, all accepted by the operator. They are folded into the sections
> they concern rather than appended, so this document still reads as one
> design of record. The substantive changes are the gateway write topology
> (writes land private, publication is an operator act), the rescoped Tier 2
> guarantee, and a set of residuals and build obligations that were missing.

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
credentials toward the org and team vaults and fans recall out across
them. The person's AI clients connect once, to the one MCP endpoint they
already have; multi-vault membership is a property of that endpoint, not
of the client configuration.

The gateway is symmetric for reading and deliberately asymmetric for
writing. One gateway connection reads across every Tier 1 vault; it does
not write across them. A write made through the gateway lands in the
personal vault, and publication into a shared vault is a separate
operator act (F3). This is said here rather than left to be discovered,
because "connect once and reach everything" is a property of recall, and
stating it as a property of writes would be false.

Five conditions are part of the decision, not implementation detail:

- **Membership credentials are OAuth 2.1 rotating refresh tokens.** The
  gateway participates in the existing grant machinery (single-use
  rotation, reuse detection, confidential-client verification, v0.1.16),
  never the non-expiring `api_tokens` path. A gateway credential that
  leaks stops working at the next rotation; a leaked static token works
  forever. Rotation also has an availability failure mode, and it is the
  failure mode of this very choice, so it is designed against rather
  than discovered. The gateway is a headless automated client: a
  rotation that is not persisted before the new token is first used ends
  with a retry on the old token, reuse detection firing, and the whole
  token family revoked, leaving a membership silently dead until a human
  redoes the grant ceremony. Three build obligations follow. Rotated
  refresh tokens are persisted atomically before first use. The Memory
  Mirror carries a membership-health surface that distinguishes "vault
  unreachable" from "credential dead, re-grant needed", because to the
  fan-out those look identical and only one of them heals by itself. And
  the access-token lifetime plus the revocation-check policy are
  specified explicitly, because they are what bounds how fast a revoked
  membership actually stops being served.
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
  Flagging costs one additive optional block on recall output (which
  member vaults answered, which did not, and why), added the way ADR-0006
  added `client`: new optional output, no input change, an additive-only
  golden surface diff. That block is part of v1 of this design, not a
  hypothetical later growth.
- **The fleet-compromise amplification risk is documented.** A gateway
  that can reach N vaults is a more valuable target than a vault that can
  reach one. This risk belongs in SECURITY.md when the feature builds
  (the SECURITY.md update happens with the build, not with this ADR).
- **Fan-out privilege rides only rotating client credentials.** The
  gateway takes membership credentials out of client configs, but every
  client still holds a credential pointed at the gateway, and the fan-out
  is what makes that credential powerful. A client credential may carry
  fan-out privilege only if it is a rotating OAuth client credential. A
  static bearer or a non-expiring `api_token` pointed at the gateway
  serves the personal vault only.

The credential blast-radius accounting has to be stated in both
directions, because it moves both ways. The number of plaintext copies
drops sharply: N vaults x M clients x K devices of static bearers
collapse to one gateway credential per client, with the membership
credentials held once, SQLCipher-encrypted, inside the person's own
instance, and rotating rather than static. The reach of a single stolen
copy rises: before the gateway, a stolen client config reached one
vault; after it, a stolen fan-out-privileged client credential reaches
the personal vault plus every Tier 1 vault the person belongs to. The
trade is favorable only because of the last condition above. Rotation
gives a stolen gateway credential a lifetime instead of a license, and
credentials that do not rotate are held to the personal vault, so wide
reach exists only where the credential expires. The amplification risk
is real, is documented, and goes into SECURITY.md at build time.

One decision inside F1 is deliberately left open and named here rather
than settled: whether member-vault hits are written into the gateway's
own substrate or passed through and served ephemerally. Persisting them
makes cross-vault consolidation possible, and it also means every org
fact a fan-out ever returned lives permanently in the person's
append-only substrate. Ephemeral serving gives up cross-vault
consolidation and shrinks that residue to what the person deliberately
re-remembered. The lean is ephemeral serving, because the residue is the
harder consequence to undo (see "What it costs"), but the decision is
taken at v2 build start with the trade recorded so it is taken
deliberately rather than by whichever code lands first.

### 2. F3: default write target, credential-pinned

The honest name for this decision is: private-by-default plus opt-in
shared defaults, per credential, with publication out of the gateway as
an explicit operator act.

A write's default destination is carried by the credential, fixed at mint
or grant time, never by a per-request or per-project hint the model
supplies. Model-supplied routing hints are rejected here explicitly as a
memory-laundering vector: an untrusted model that can choose the
destination of a write can redirect a confidential fact into a shared
vault, and no storage-layer control can undo that after the fact. The
default target is the person's private vault unless the operator
explicitly configures a specific credential to default to a specific
shared vault.

That rule fixes the write topology, and the topology is stated in full
here rather than left implicit. A credential pins one destination, and
on MCP every tool argument is model-composable, so there is no
wire-level way for a single connection to write into two vaults without
handing the model the choice. Two write paths follow, and they are the
whole set.

**Through the gateway, a write lands in the personal vault, always.**
Publication into a shared vault is a separate, explicit operator act on
the Memory Mirror: the person sees the write that landed privately and
publishes it into the shared vault, or does not. This generalizes the
hold-and-surface flow described below from an exception a heuristic
triggers into the normal path. It costs one step per shared fact and
buys two things worth more than the step: the model never selects a
destination, and a human is in the loop for every byte that leaves the
person's own vault.

**Directly, an org-credentialed client writes straight into the shared
vault it is pinned to.** This is the ADR-0010 org-direct-connect model
shipped in v0.1.27, and it is the frictionless path for tooling that
legitimately belongs to the organization: an org agent, a CI job, a
member's dedicated work client. Its consequence is write monogamy, and
that belongs in product copy in exactly those terms: such a client
writes to that one vault and to no other for as long as it holds that
credential. A person who wants one client writing to their personal
vault and another writing to the team vault configures two clients, not
one client with two moods.

`observe` is not routed at all: it always pins to the personal vault.
Observation is automated, high-volume, and never reviewed before it
lands, so a shared default would push a person's whole agent activity
stream, including whatever personal work happened in the same session,
into a team vault. That is a categorically worse exposure than one
dictated sentence, and it would drown the Mirror's shared-write view in
the process. Org-relevant activity reaches an org vault the way
org-relevant writes do, through an org-credentialed agent (ADR-0010),
where the credential belongs to the organization and the volume is the
organization's own.

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
- **The residual hazard is named.** A person working through a client
  that is credential-pinned to a shared vault who dictates something
  confidential lands it in that shared vault, with no promotion step in
  between. The gateway path does not carry this hazard, because nothing
  leaves the personal vault without an operator act; the direct
  org-credentialed path does, and that is the path a working team uses
  most. Mirror surfacing detects this after the fact; it does not contain
  it. Other members' AI sessions may already have read the fact before the
  supersession lands. Detection is not containment, and this ADR does not
  pretend otherwise. The structural answer to that hazard is the next
  decision.

Condition b has a federated wrinkle that must be discharged before build.
ADR-0006 derives the `client` slug from the credential the vault itself
validated and forbids taking it from anything the caller supplied. A
gateway-forwarded write breaks that cleanly: the destination vault
validates the person's membership credential, so every forwarded write
would record "person X's gateway" whatever tool actually held the pen,
and the question ADR-0006 exists to answer goes dark for precisely the
writes that cross an organizational boundary. The same gap applies to
`routed_by`, which the destination cannot derive from any credential it
checked. The answer is not to relax ADR-0006's rule but to name a third
category under it: the gateway as an authenticated, semi-trusted
forwarding tier whose asserted originator and routing facts are recorded
distinctly from credential-derived ones, so a reader can always tell
which kind of claim they are looking at. Writing that amendment to
ADR-0006, including which fields the destination sidecar records and how
recall serves them, is a precondition of the v2 build. This ADR records
the obligation and does not edit ADR-0006.

The residual hazard splits into two laundering kinds, and F3 closes only
the first. Destination-laundering is closed: the model cannot choose
where a write goes. Content-laundering is not: the model still composes
what bytes go into a write to a legitimately pinned shared vault. The
adversarial shape is EchoLeak (CVE-2025-32711): privileged content (a
personal-vault recall) plus untrusted content (a poisoned shared-vault
memory) plus a compliant write path out.

The v2 build carries a named mitigation for content-laundering, and the
Mirror-promotion topology changed its job. The check can only run where
one component sees both sides of a laundering move, the bytes served
into a session and the bytes leaving it, and the only such component is
the gateway. Under the promotion topology the gateway's outgoing shared
writes are Mirror publications, so the check becomes decision support at
the moment a human is already in the loop: a publication whose content
reuses spans served from another member vault is flagged as such before
the person confirms it. The direct org-credentialed write path cannot be
span-checked by any afair component at all; the org vault never sees
what other vaults served into the session, and the gateway never sees
the write. That path's safeguards are write monogamy (F3), the org-side
Mirror visibility of recent writes, and Tier 2, and naming that plainly
is part of the boundary below. This is a design commitment for the v2
build, not something shipping now:

- The check is span-level provenance matching, not generic similarity.
  The gateway compares a publication leaving it against the actual bytes
  it served into the session from other member vaults and looks for
  verbatim or near-verbatim reuse of served spans. Entity overlap and
  embedding proximity were considered first for this job and rejected:
  for anyone whose personal vault covers their working life, a
  legitimate team write overlaps in entities and embedding space with
  the same session's personal context almost by definition, so a low
  threshold queues every shared write for approval (which is the
  rejected strict alternative wearing a different name) and a high
  threshold is dead code. The two failure modes may bracket an empty
  middle, which is why the mechanism is span reuse: it has a defensible
  false-positive story and a threshold that can be written down.
- On a match, the publication is flagged before it is confirmed: the
  Mirror shows which served spans, from which vault, appear in the fact
  about to be published, and the writer, who is the data subject,
  publishes or keeps it private with that in view. Other members never
  see the content before that decision, because no publication reaches a
  shared vault without one; the check adds sight to a gate that already
  exists rather than building a new one.
- The direct path keeps its visibility mechanism: a Memory Mirror
  "recent writes to shared vaults" view, fed by the person's own read
  access to the vaults they belong to, so the owner sees what their
  org-credentialed clients wrote. This also closes the
  mis-route-visibility gap left by condition c: a mis-routed write is
  only fixable by supersession if the owner sees it.

The boundary is stated so the mechanism is not false comfort: this makes
Tier 1 defense-in-depth, not leak-proof. Two bypasses are known and
neither closes at this layer. A model that rewords served content before
writing it defeats span matching. And the check can only compare against
bytes the gateway itself served, so content that reached the model any
other way, through a direct connector, a pasted document, or a poisoned
memory that tells the model to answer from what it already holds without
calling recall, gives it nothing to match. The direct org-credentialed
path is not checked at all, as stated above. The
structural guarantee remains Tier 2, where the confidential vault is not
on the gateway at all; anything that truly must not leak belongs in
Tier 2. On the invariants: the write is always recorded append-only
(I2), the hold is a serving-and-forwarding overlay and never a substrate
mutation, the frozen verbs are unchanged (I1), and the owner stays in
control (I4).

### 3. The two-tier confidentiality model

Once one AI session has read two vaults, nothing at the storage layer can
prevent content from the first appearing in a summary written to the
second: the laundering happens in the context window, outside afair's
reach. The only control that works is structural: make sure the session
never held both. How much of that afair can itself enforce, and how much
falls to how the person configures their clients, is scoped honestly
below.

- **Tier 1 (convenience):** the personal vault and shared team vaults ride
  the F1 gateway on one connection. Cross-pollination between these vaults
  through the model's context is an accepted cost of the convenience; the
  membership boundary here is organizational, not adversarial.
- **Tier 2 (confidential):** a confidential circle is direct-connect only.
  The gateway never holds its membership credential, so a gateway session
  structurally cannot read it: the token is not there. Leadership, board,
  and anything that must stay dark from the rest of the org lives in
  Tier 2.

The Tier 2 guarantee has to be stated at its true scope, because the
obvious wording overstates it. What Tier 2 guarantees is gateway-scoped,
and it is worth having: the gateway cannot serve confidential content
because it holds no credential for it, and a compromised gateway reaches
nothing in Tier 2. That is structure, not policy, and no pure
aggregator can offer it.

What Tier 2 does not guarantee is session-scoped separation. Hosted
clients support several concurrent MCP connections, and the natural
place to put a direct Tier 2 connector is the same claude.ai account,
the same Claude Code instance, or the same editor profile that already
holds the gateway connector. Once both connectors live in one client,
every session in that client holds both, and the context-window
laundering described above applies in full and in both directions: a
poisoned Tier 1 memory can induce a write through the Tier 2
connector's legitimate credential, and Tier 2 content can be laundered
into a Tier 1 shared write. Nothing afair controls prevents that. It
depends on the person keeping Tier 2 connectors in a separate client
workspace, which is the "remember to open the right session" posture
this design cannot fully escape after all. That is the residual, named
in the same register as the F3 dictation hazard: containment is
structural at the gateway and configurational at the client, and only
the first half is ours to enforce. Two build obligations shrink the
second half. Product copy states the separate-client-workspace rule as
part of what Tier 2 means, not as a footnote. And a Tier 2 vault's
server instructions warn the model when the session's visible toolset
spans a Tier 2 connector plus any other memory connector, so a mixed
session at least says out loud what it is holding.

Tier 1 co-residency implies mutual trust, and that sentence has a
direction the "organizational, not adversarial" framing above hides. The
gateway aggregates every Tier 1 membership one person holds, including a
consultant's two unrelated client organizations, and between those two
the boundary is adversarial in exactly the way it is not inside one
company. Content recalled from one member vault and published into
another is the same laundering shape as the personal-to-shared
direction; the publication-time span check covers it identically, and on
the direct org-credentialed path it is as unchecked as every other
direction (F3). So the rule is stated rather
than assumed: placing a vault in Tier 1 means accepting co-residency
with every other Tier 1 vault that person belongs to. An organization
unwilling to accept that gets a lever it currently lacks: a membership
grant may be marked Tier 2 only, and a Tier-2-only grant may not be
installed into a gateway at all. That is the org-side counterpart to the
person-side tier choice, and it is how an organization buys structural
separation instead of trusting the shape of a person's other
memberships.

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
vault and it drops off the aggregator within the access-token lifetime,
and at once where the revocation check specified in F1 fires. The word
is bounded rather than instant because outstanding access tokens stay
valid until they expire, and that bound is a build parameter to be
chosen, not a hope. Moving the other way,
confidential to convenience, widens exposure and therefore requires
deliberate operator ceremony. The trap avoided is a creation-time-only
choice, which would force re-creating the vault to change its tier;
that is the shape of ChatGPT's project-only-memory setting, settable
only at project creation and never after.

One tension between Tier 2 and afair's recall honesty is left standing
deliberately. A gateway recall answers "what do I know about X" over the
vaults it can reach, which is Tier 1 only. Saying so in the response
("confidential circles excluded") would leak the existence of Tier 2
memberships into every session, including the sessions where the whole
point of Tier 2 is that nobody learns it exists. The rule is therefore
that the gateway's recall honesty is scoped to the vaults it can reach:
it reports which reachable vaults failed to answer (F1 condition c), and
it never discloses, enumerates, hints at, or counts Tier 2 memberships.
The person knows what they put in Tier 2; the session does not. That
scoping is a decision, not an oversight.

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

One condition binds both, and it is where federation could quietly
become a hosted-only capability. The invite, grant, and membership
protocol is part of the open core, lives in this repository, and speaks
vault to vault. A self-hosted personal vault can join a hosted
organization, and a self-hosted organization can invite anyone, with
neither side needing an account on the hosted control plane. The control
plane may broker the ceremony as a convenience, the way a hosted mail
provider brokers SMTP without owning it, but it is never the thing that
makes federation work. Grant machinery that existed only in the private
control plane would make federation the first afair capability that is
structurally hosted-only, which I4 forbids.

## Invariant integrity

- **I1 (surface stability).** The three frozen verbs do not change on
  their input side. Membership is otherwise a deployment and
  credential-topology concern, not a wire-surface concern: the gateway
  speaks the same remember, recall, and observe to its clients and to its
  member vaults. One additive output growth is part of this design rather
  than hypothetical: recall gains an optional block naming which member
  vaults answered and which did not (F1 condition c). It is
  additive-only, passes the golden surface guard, and follows exactly the
  path ADR-0006 took when it added the `client` output field without
  touching any input. No routing argument is ever added, and that is a
  security decision rather than a scope decision: on MCP every tool
  argument is model-composable, so a routing parameter is a
  model-supplied routing hint whatever human intent sits behind it, and
  F3 rejects those. Write destination stays a property of the credential
  and of the operator's promotion act.
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

- One connection per person per client reads across every Tier 1 vault
  they belong to; cross-vault recall exists for the first time. Writing
  across them is deliberately not part of that (F3).
- Credential sprawl collapses: N x M x K plaintext token copies become
  one encrypted, rotating credential store per person, at the price of
  raising what a single stolen client credential can reach (F1).
- A departing employee keeps their personal vault (F5), and an org's
  confidential material has a structural home (Tier 2) instead of a
  policy-only one.
- Write routing is deterministic and auditable: the credential decides,
  the operator publishes, the sidecar records, and a wrong default is
  visible as `routed_by=default` in provenance.

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
- Publishing a fact into a shared vault through the gateway costs an
  operator step. That is the price of never letting a model choose a
  destination; teams that want frictionless shared writing use
  org-credentialed direct connect and accept its write monogamy (F3).
- The Tier 2 guarantee is structural at the gateway and configurational
  at the client. The second half depends on the person keeping
  connectors in separate client workspaces, which afair cannot enforce.
- Offboarding leaves residue, and the residue is permanent by
  construction. Revocation (F4) severs future access and does nothing
  about the past: whatever a gateway pulled from an org vault while the
  membership was live, and whatever was derived from it, sits in the
  person's own append-only substrate (I2), owned solely by the person
  (I4), with the organization contractually refused any reach into it
  (F5). That composition is invariant-coherent, and it is the first
  question an organization's counsel will ask, so it is written down here
  rather than found in a security review. The one lever that changes its
  size is the fan-out persistence decision named in F1: ephemeral serving
  shrinks the residue to what the person deliberately re-remembered, at
  the cost of cross-vault consolidation.

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
  not in the trust position to make it. The rejection is permanent and
  covers any future routing parameter on the wire surface, because a tool
  argument is model-composable no matter who dictated its value.
- **Strict private-by-default with no shared defaults at all.** Every
  shared-vault write, on every path, would need an explicit per-write
  operator action. Rejected in that total form as fatal B2B friction: a
  team vault nobody can conveniently write to is a team vault nobody
  writes to. What the design keeps is the operator act exactly where the
  destination is genuinely a judgement call (publication out of the
  gateway) and a frictionless path where the credential has already
  answered the question (org-credentialed direct connect, monogamous by
  construction). Both halves preserve the safety property that the model
  never chooses.
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
- **Hosted-client protocol evolution.** Hosted MCP clients already
  support multiple concurrent server connections; what they do not offer
  is isolation between them, so N connections per person per device
  remains poor UX and, as the Tier 2 residual shows, buys no session
  separation. If clients gain first-class per-connection workspace
  isolation, two things move at once: the gateway's convenience case
  weakens (direct connect per vault becomes viable UX) and the Tier 2
  residual becomes enforceable. F1's topology and the Tier 2 build
  obligations are both revisited then, before more is built on either.
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

Four questions are open and are named so they get decided at build
start rather than settled by whichever code lands first:

- **Cross-vault score merging.** Relevance scores come from independent
  per-vault embedding indexes and are not comparable across vaults. The
  merge rule for a fan-out result set has to be chosen and written down.
- **Hit identity and routing back.** A `by_id` or `by_content_hash`
  follow-up on a fan-out hit needs the gateway to route back to the
  owning vault, so served hit identifiers have to become
  vault-qualified in some form that stays additive on the wire (I1).
- **Cross-vault references.** `parent_hashes`, entity references, and
  linked event ids inside a served hit point into the vault that
  produced them and dangle everywhere else. Serving them honestly,
  either resolvable or explicitly marked out-of-vault, is a design
  choice and not a detail.
- **Fan-out persistence.** Whether member-vault hits are written into
  the gateway substrate or served ephemerally, per F1, where the lean is
  ephemeral and the offboarding consequence is spelled out.
