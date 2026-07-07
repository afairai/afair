# ADR-0006 - Client provenance lives in an out-of-hash sidecar, not in origin

> **Status:** Accepted
> **Date:** 2026-07-07
> **Audience:** anyone touching the event write path, `origin`, the content-hash contract, recall serving, or the export allowlist
> **Relates to:** VISION.md §4 (I1 additive surface, I2 append-only + content-addressed, I4 user owns the substrate), [ADR-0002](ADR-0002-belief-revision-derived-layer.md) (entrenchment tiers, the review loop that `asserted_by` defers to), [ADR-0005](ADR-0005-telemetry-retention.md) (the memory-vs-operational line this table sits on the memory side of), issue #29

## Context

A vault is written to by many clients: Claude Code, Claude.ai, Codex, Cursor,
a phone. Until v0.1.17 every MCP-initiated write stamped the same coarse
`origin="agent"`, and the question "which of my AI tools wrote this memory"
had no answer. The authenticated client IS known at the transport layer
(the static bearer, an API token with a label, an OAuth client registered
with a `client_name`), but that identity was discarded the moment the
middleware finished.

The obvious fix, refining `origin` per client, was in fact the documented
plan: a comment next to `DEFAULT_ORIGIN` in `afair/mcp/handlers.py` promised
"per-client refinement in a later phase." That plan hits a hard fork:

- **`origin` is part of event identity.** `events.content_hash` hashes
  `(kind, origin, payload, parent_hashes)`; the docstring says so explicitly.
  Two writes of the same text from two clients would hash differently, so
  the dedup that today collapses them into one event (returning
  `deduplicated=true`) silently breaks. Worse, the same text from the same
  client before and after the change would also hash differently: refining
  `origin` changes the identity semantics of every future event.
- **The OAuth token carried no client identity anyway.** Dynamic client
  registration stores `client_name` (capped at 80 chars), but the JWT claims
  were `iss/aud/sub/exp/iat/jti/email` only, so even a willing consumer had
  nothing to read.

So the real question is: where does per-client provenance live, if not in
`origin`?

## Decision

**Keep `origin` coarse and event identity untouched. Stamp the authenticated
client into `event_provenance`, an append-only sidecar keyed by `event_id`
and OUT of the content hash.** This supersedes the origin-refinement plan;
the `DEFAULT_ORIGIN` comment now says why.

The sidecar follows the same overlay discipline as `edge_confidence_scores`
and `edge_serves`: the base row (the event) never changes, and the current
view is composed at read time. Concretely:

1. **Schema.** `event_provenance(id, event_id, client, auth_kind, verb,
   stamped_at)` with `UNIQUE(event_id, client)`, indexed by `client` and
   `event_id`, and `no_update`/`no_delete` triggers from day one (I2). No
   backfill and no migration: absence of a row means the event predates
   provenance or was written outside an HTTP request (unit tests, cold-path
   workers). Implementation in `afair/substrate/provenance.py`.
2. **Identity is credential-derived only.** The `client` slug comes from the
   credential the middleware already validated, never from client-supplied
   headers or tool arguments: the static bearer stamps `("master",
   "master")`; an API token stamps its sanitized label; an OAuth token
   stamps the sanitized `client_name` claim (newly minted into the JWT on
   both the auth-code and refresh grants; legacy tokens without the claim
   fall back to `"oauth"`). Local no-auth self-host mode stamps
   `("local", "none")` so self-hosters get provenance too. The sanitizer
   (`client_slug` in `afair/mcp/auth.py`) lowercases, keeps
   `[a-z0-9._-]`, and caps at 64 chars. No session ids, no tool-call ids,
   no request headers are recorded (data minimization, I4/I8).
3. **Write semantics.** After each `remember`/`observe` event write the
   handler stamps one row via `INSERT OR IGNORE`, deliberately OUTSIDE the
   `was_inserted` branch: a dedup'd re-write from the SAME client is a cheap
   no-op, while a DIFFERENT client writing the same content-hashed event
   appends a second, honest row. The stamp is fail-soft: a provenance
   failure logs a warning and never fails the underlying write.
   `recall(decide=...)` correction events already carry `origin="user"` and
   are out of scope.
4. **Serving (additive, I1).** `RecallHit.client` carries the earliest stamp
   (the author) at `standard`/`full` verbosity and on `by_id` /
   `by_content_hash` fetches; `compact` omits it and adds zero queries.
   `recall(stats=True)` gains `by_client` (distinct events per client), a
   different axis from the untouched `by_origin` (user/agent/worker). The
   golden MCP surface changes additively and the diff is the proof.
5. **Guard rails.** `event_provenance` is on the Pruner's never-touch list
   (it is durable memory-adjacent provenance, not ADR-0005 telemetry) and on
   the export allowlist, so provenance rides the user's vault export (I4).

**The boundary with caller-asserted provenance.** The same issue shipped an
optional `asserted_by: "user" | "model"` field on `remember`. That field is
part of what the caller SAID, so it goes into the payload and therefore into
the hash (the same sentence asserted-as-user and asserted-as-model are
different assertions). The sidecar fork in this ADR applies only to
server-derived transport facts, which the caller never uttered. `asserted_by`
is advisory: it maps to an entrenchment tier that by construction buys
nothing at the auto-confirm gate (`assertion_entrenchment` in
`substrate/belief.py`), so a self-reported "user" can never manufacture
operator-grade trust; that is earned only through the `recall(decide=...)`
review loop (ADR-0002).

## Consequences

- **Dedup and the hash contract are preserved.** Identical content from any
  number of clients stays one event; the sidecar records each writer
  honestly. No pre/post hash regime split, no identity change.
- **Legacy events serve exactly as before.** No provenance row means no
  `client` key on the hit; nothing errors, nothing is backfilled (I3: a new
  view over unchanged substrate).
- **Multi-writer provenance is honest by design.** A second client stamping
  a dedup'd event is recorded as a second row; `read_event_provenance_batch`
  orders by `stamped_at` so the first row is the author.
- **The JWT grew a claim additively.** Old access tokens validate unchanged
  and serve as `"oauth"`; new mints carry the sanitized `client_name`.
- **A maintenance rule to keep.** Stamping must stay OUTSIDE any
  `if was_inserted:` block; symmetry-copying it inside would silently lose
  the second-client row on dedup'd events. The tests lock this.

## Alternatives considered

**Option A: refine `origin` per client.** The originally planned path.
Rejected: `origin` is inside `content_hash`, so per-client origin breaks
dedup across clients and silently changes the identity of every future
event. The failure is invisible at write time (writes still succeed) and
permanent at rest.

**Option B: refine `origin` and exclude `origin` from the hash.** Keeps
dedup, but splits the vault into two hash regimes: every pre-change event
was hashed WITH origin, every post-change event without. Old hashes can no
longer be recomputed from the stated rule, `parent_hashes` references
straddle two regimes, and the content-addressed promise of I2 stops being
one sentence. Rejected as I2/I3-hostile.

**Option C: inject the client into the payload.** In-hash, so it breaks
dedup exactly like Option A, and it additionally plants server-derived bytes
inside what is supposed to be the caller's content. Rejected.

**Option D: the out-of-hash sidecar.** Chosen. It is the established overlay
pattern in this codebase (`edge_confidence_scores`, `edge_serves`), costs
one `INSERT OR IGNORE` per write, and leaves every existing contract intact.

## Invariant fit

- **I1**: the three verbs are untouched; recall output gains two optional
  fields (`client`, `by_client`) and `remember` one optional param
  (`asserted_by`). The golden surface diff is additive-only.
- **I2**: `event_provenance` is append-only from day one (triggers,
  `INSERT OR IGNORE`); no existing row is mutated, no backfill runs.
- **I3**: provenance is a new view over unchanged substrate; legacy events
  are served as before.
- **I4/I8**: provenance never leaves the vault except through the user's own
  export, where it now rides; the slug is credential-derived only.
- **I5**: no provider-specific code.
- **I6**: `client` is a credential slug and `asserted_by` a two-value
  provenance label; neither feeds extractor kind logic.
- **I7**: no self-modification surface is touched.
