# Project memory: afair

> Read `VISION.md` first. It is the operating constitution. The invariants in §4 are inviolable.
> This file is for project-specific working rules that complement (never override) the constitution.

## 0. Current state

**Status:** In daily real-world use. The substrate, the frozen 3-verb MCP
surface, the cold-path agents (including the relevance-decay/temporal worker),
the entity graph, the recall honesty layer, and the recursive self-improvement
loop are all live. The repo is public (open-core, AGPLv3); current focus is
distribution.
**Audience:** solo build; future contributors

### 0.1 What's live

All of this is in daily real-world use.

- **Substrate** (`afair/substrate/`): append-only, content-addressed vault: a
  SQLite event store (FTS5 + sqlite-vec) plus a filesystem blob store for large
  or binary content, encrypted at rest (SQLCipher + AES-GCM). I2 enforced by DB
  triggers.
- **MCP surface** (`afair/mcp/`): the three frozen v1 verbs
  (remember / recall / observe) over Streamable HTTP, OAuth 2.1 + scoped-bearer
  auth, multi-modal content (text/PDF/audio/image), streaming blob upload,
  async vault export, per-event client provenance (ADR-0006), host
  canonicalization on managed vaults. The wire contract is locked by the
  golden surface guard (see §4).
- **Cold-path agents** (`afair/agents/`): extractor, extraction retry,
  salience, mode-switcher, surprise, temporal (relevance-decay), entity
  canonicalizer + emergent entity graph, edge scorer (ADR-0004), schema
  evolver (emergent ontology, ADR-0003: proposes kind revisions from usage;
  the operator confirms/rejects/reverts through `recall(decide=)`), conflict
  resolver, consolidator, living synthesis worker (ADR-0007: automatic
  evidence clusters with cited, revisable syntheses), pruner, expectation
  checker (observability), and the recursive self-improvement loop (tuner +
  multi-vendor judge + rollback monitor).
- **Hosted layer**: per-user single-tenant Fly machines (I8), provisioned and
  retired from afair-web. Each user gets a per-user vanity host
  (`<name-suffix>.mcp.afair.ai`, suffix derived from their Clerk id). There is
  no bare `mcp.afair.ai` vault; every vault is its own per-user app.
- **Deploy**: product CI in this repo (`.github/workflows/ci.yml`: lint/type/
  test, no deploy). The fleet deploys from the private **afair-web** repo
  (`deploy-afair-fleet.yml`, pinned ref); a `vX.Y.Z` tag here triggers it. The
  operator runbook lives in afair-web; self-host docs are `docs/self-hosting.md`.

### 0.2 Recently shipped + current focus

The v0.1.4 to v0.1.8 hardening era (the ADR-0004 edge-confidence model,
ADR-0003 Phase 2 made effective, the dedup operator-override and
pending-review-nudge fixes, the public open-core split) is settled history;
per-release detail lives in the CHANGELOG. Since then eight more releases
shipped (v0.1.10 to v0.1.17, all live on the fleet):

- **Observability Phase 0.5 + the silent-failure batch (v0.1.10, v0.1.11).**
  The `expectation_checker` cold-path worker makes silent pipeline failures
  visible: every 15 min it counts stuck, retry-exhausted, and permanently
  failed extractions into the `observability_snapshots` table; `/health`
  serves `version` + a counts/ages/booleans `pipeline` block + per-worker
  `seconds_since_last_success` (200 on a backlog, 503 only when the DB is
  down); `pipeline_events.timeline()` answers "where did event X get stuck".
  The P0 batch then closed the failure modes the checker exposed: every
  extractor failure branch records `extraction.failed`, a failed extraction
  no longer shadows an earlier success or starves canonicalizer selection,
  `open_threads` reads from consolidation events, and observe intake
  truncates oversized extras.
- **Recall at scale + the pending-corrections root fix (v0.1.12, v0.1.13;
  ADR-0005 `Accepted`).** Recall gained verbosity levels (compact default),
  a limit cap, cursor pagination, batch `decide`, and pending-queue
  pagination, plus null-free serialization and extractor output caps. P2a
  added `worker_watermarks` (mutable re-scan cursors with a lagged
  never-skip frontier) so cold-path workers stop re-scanning history, a
  UNION entity-match rewrite, a session-start salient index, and a capped
  anyio thread limiter. ADR-0005 reclassified `pipeline_events` /
  `observability_snapshots` as prunable operational telemetry: their I2
  triggers are retired and the Pruner ages rows out past
  `telemetry_retention_days` (default 90).
- **The schema regression + the golden surface guard (v0.1.14, v0.1.15).**
  A `| str` param widening had leaked a top-level `anyOf` that broke
  claude.ai tool surfacing while all tests stayed green; v0.1.14 restored
  the clean inputSchema, hardened remember intake (invalidation-target
  validation, truncate-preserve context/type_hint, naive-timestamp
  normalization), and fixed a batch of cold-path correctness bugs. v0.1.15
  locked the wire contract: `tests/goldens/mcp_surface.json` + shape lints
  fail any non-additive surface diff (registered in §4).
- **Noise reduction: the review-queue churn root fix (v0.1.15).** Four
  linked parts: reviews are serve-gated (an edge earns a review slot only
  after a recall actually served it; new append-only `edge_serves` table)
  and never-served low-confidence edges auto-expire past a grace anchored
  to a serve-tracking epoch, so a deploy can't mass-retire the legacy
  graph; write-time gates stop noisy edges being created (no edges from
  observe events, confidently-transient sources, or non-durable
  predicates), with a bounded retro-sweep for the existing graph;
  structural-junk kind-review proposals are suppressed; and `decide` honors
  `to_kind` on confirm, not only on reject.
- **P2 hardening (v0.1.16).** Compound intake: per-part spill to the object
  store (FTS still covers spilled text), a sum-of-parts byte cap, message
  flattening under the extractor char budget, compound grounding so
  compound relations stop being dropped, and a queryable
  `pdf_no_text_layer` marker. OAuth: a transport-only host-canonicalization
  middleware (`afair/mcp/host_canon.py`), and the refresh-token grant now
  binds to the presenting client, verifies confidential-client secrets,
  rotates single-use, and detects reuse. P2e hygiene: dead tunables
  removed, registry fallbacks narrowed, worker transactions rolled back on
  failure, dead code deleted.
- **Event provenance (#29, ADR-0006 `Accepted`, v0.1.17).** "Which of my AI
  tools wrote this memory" is now answerable: every HTTP write stamps the
  credential-derived client into the append-only `event_provenance` sidecar
  (out of the content hash, so dedup holds; `origin` stays coarse). Recall
  hits carry `client`, stats gain `by_client`, and provenance rides export
  (I4). Recall at `verbosity="full"` also serves the durability rationale
  (salience + components + a `why_durable` line), and `remember` gained an
  optional advisory `asserted_by: "user" | "model"` field that records who
  asserted a fact but by construction can never raise trust
  (operator-grade trust still comes only from `recall(decide=)`).

**Current focus: Personal design partners.** The living-synthesis worker,
end-to-end memory-quality gate, read-only Memory Mirror, and direct-to-vault
import path are ready for a 15-person, six-week cohort. The Mirror is
becoming actionable: pending corrections and conflict flags are decidable
from the dashboard through the same `decide_correction` entry that serves
`recall(decide=)` (operator conflict resolution: ADR-0008). Broad distribution
waits for two consecutive green weeks on the private launch gates. Show HN
remains hand-written because HN requires human-written text.

### 0.3 Blocked

- Nothing.

### 0.4 Captured for later (not active build)

- **Vault Dashboard**: read-only insight surface on the control plane
  (entity-graph hero, surprise heatmap). After the daily-use window.
- **Early-access signup professionalization**: at 50 to 100 signups: dedicated
  store, double opt-in, admin broadcast.
- **Funding stance**: bootstrap-default / VC-conditional (private note).
## 1. Naming (post-rebrand)

`afair` is the **final product name**. `afair.ai` is registered. Code,
repo, MCP server, Fly apps, Docker image all use it. The earlier
codename phase is over.

Working rule for code: keep imports relative (`from . import substrate`)
rather than absolute (`from afair.substrate import ...`) so the package
directory could be renamed later without churn, but no rename is
planned.

## 2. Stack

| Concern | Choice | Reason |
|---|---|---|
| Language | Python 3.12+ | Memory/agent research ecosystem is Python; FastMCP is Python-first |
| Package manager | `uv` | Fast, modern, lockfile-stable |
| MCP framework | FastMCP | Most mature Python MCP server |
| MCP transport | Streamable HTTP only | Required for Claude.ai connectivity; works equally well on localhost |
| HTTP layer | FastAPI (under FastMCP) | Non-MCP endpoints (health) |
| Storage | SQLite + FTS5 + sqlite-vec, plus a filesystem blob store | Transactional, portable, inspectable; blobs hold large/binary content |
| LLM | litellm wrapper, default `anthropic/claude-haiku-4-5`; per-agent overrides (`CANONICALIZER_MODEL`, `ENTITY_DEDUP_MODEL`, `CONFLICT_RESOLVER_MODEL`, `CONSOLIDATOR_MODEL`, `ENTITY_ARTICLES_MODEL`, `TEMPORAL_MODEL`, `JUDGE_PANEL`) fall back to `EXTRACTOR_MODEL` / the built-in judge panel | I5 vendor neutrality; VISION §6.5 heterogeneous models per agent |
| Replication (managed) | LiteFS | Deferred to Phase 8 |
| Validation | Pydantic v2 | At every boundary, parse-don't-cast |
| Logging | structlog | JSON with PII redaction |
| Deployment | Docker → Fly machine | Single binary across local / self-hosted / managed |

## 2.5 Public-repo discipline (binding)

The open-source core (this `afair` repo) is **AGPLv3** (see `LICENSE` and
`VISION.md` §12). The hosted control plane (`afair-web`: provisioning,
billing, dashboard) is a **separate private repo** and never merges here.
The operational rule stands:

> **Build as if the repo is public, because the core is.**

Concretely:

- No secrets, credentials, tokens, or API keys in committed files. Use
  `.env.local` (gitignored) for local secrets; keep durable backups of any
  keys outside the repo.
- No internal jokes, no slurs against competitors, no offhand snark
  in code, comments, or commit messages.
- No proprietary algorithmic tricks that depend on staying secret for
  their value. The moat is single-tenant + cross-vendor + brand,
  not hidden code.
- No lock-in mechanisms: no anti-export, no phone-home, no kill-switch
  patterns. I4 says the user owns the substrate; the code must reflect
  that even when it is our own running production code.
- Telemetry minimal, off-by-default, anonymized when on. If we add
  Sentry/PostHog/observability later, document it in the README and
  provide an opt-out.
- Tests + docs at every layer. A stranger reading the repo for the
  first time should be able to understand what each module does and
  why it exists.
- Commit messages reviewable by a stranger: conventional commits,
  imperative voice, no "fix the thing" subjects.
- Transitive dependencies stay AGPL-compatible (Apache2/MIT/BSD). No
  incompatible-copyleft deps that would conflict with the AGPLv3 release.

Anything genuinely business-internal (funding strategy, competitor
teardowns, marketing positioning, hosted-control-plane design) lives in
gitignored local files or in the private `afair-web` repo, never in this
one. See the `.gitignore` "Business-internal strategy docs" block.

## 3. Trust ladder (binding for all phases)

1. **Phase 0**: explicit `remember` / `observe` calls only. Zero automated ingestion.
2. **Phase 1+**: user-initiated manual import (paste, drag, "ingest this URL").
3. **Phase 2+**: opt-in connectors, one source at a time, salience filter auditable before enable.
4. **Continuous sensing** (Gmail, Calendar, Slack, Drive): earned, not assumed at install.

If a feature proposal requires accessing user data the user hasn't deliberately handed in, the answer is no until the trust ladder reaches that rung.

## 4. Documentation registry

| File | Purpose | Update cadence |
|---|---|---|
| `VISION.md` | The constitution: vision, mission, invariants, architecture, durable product boundaries, research grounding | Quarterly review; treat as zeitlos, no per-phase status updates |
| `docs/adr/ADR-0001-constitutional-invariants.md` | Why the eight invariants exist + why drawn this way: each negates a failure mode, the three reinforcing chains, the I2-erasure clarification, and the accepted bets (I8 economics, I3 projection discipline, I7 aspirational). Has re-examination triggers | When an invariant is re-examined or a bet resolves |
| `docs/adr/ADR-0002-belief-revision-derived-layer.md` | Treat the entity graph as defeasible beliefs: entrenchment trust tiers (AGM), source-cascade retraction (JTMS/defeasible), evidence-grounding (= the relation fix), quarantine + auto-confirm policy (KG HITL), correction-on-recall (reconsolidation). Grounded in cited papers. Drove the `edge_reviews` table + `substrate/belief.py` | When the trust model / confirm-loop changes |
| `docs/adr/ADR-0003-emergent-ontology.md` | Proposed design that discharges the I6 debt: entity kinds become an append-only registry (add/rename/merge/split/deprecate revisions), kind decouples from entity identity (v2 IDs + kind-assignment overlay + resolution views, no migration), and the VISION §6.5 Schema-Evolver ships as a propose-only cold-path worker gated by the ADR-0002 `recall(decide=)` confirm loop | When the ontology model / Schema-Evolver design changes |
| `docs/adr/ADR-0004-edge-confidence-model.md` | Replaces the flat 0.8 edge confidence with a transparent log-odds model (write-time prior in the `confidence` column + append-only `edge_confidence_scores` overlay, latest-wins with column fallback). Wires the consumers: discriminating auto-confirm floor, per-edge served `confidence` + low-confidence caveat in recall, the `edge_review` proposal queue (first production caller of `record_edge_review`), the article-synthesizer filter, and three bounded tuner tunables with `calibration_report` as the evidence. Legacy edges never mutated (I2/I3); scored by the cold-path `edge_scorer`. Low-confidence edges served with a caveat, not suppressed | When the confidence model / its consumers change |
| `docs/adr/ADR-0005-telemetry-retention.md` | Classifies `pipeline_events` + `observability_snapshots` as OPERATIONAL TELEMETRY (the pipeline's flight recorder), not user memory: I2 protects the memory substrate, not regenerable instrumentation. Retires the four append-only triggers on ONLY those two tables (idempotent `DROP TRIGGER IF EXISTS`; fresh vaults never create them) and lets the Pruner age rows out past `telemetry_retention_days` (default 90). Draws the memory-vs-telemetry line explicitly and durably (VISION §4 cross-ref); a one-way relaxation reversed by re-adding the triggers | When the memory/telemetry line or the retention window changes |
| `docs/adr/ADR-0006-event-provenance.md` | Client provenance lives in an out-of-hash sidecar, not in `origin`: `origin` is part of the event content hash, so per-client refinement would break dedup and fork the hash contract. Instead every HTTP write stamps the credential-derived client into the append-only `event_provenance` table (no backfill; absence = pre-provenance or non-HTTP write); serves `RecallHit.client` + stats `by_client`, rides export (I4), Pruner never-touch. Also draws the caller-asserted boundary: `asserted_by` is content (in-payload, in-hash, advisory, can never raise trust) | When the provenance model or its consumers change |
| `docs/adr/ADR-0007-emergent-living-syntheses.md` | Replaces the entity-only topic axis with deterministic automatic clustering over entity recurrence, strong semantic links, and explicit lineage. The model labels and summarizes selected evidence but cannot choose a fixed category or add sources. Stable cluster identity, split/merge ancestry, citations, append-only supersession, hub suppression, and bounded cycles preserve I2/I3/I6/I7. Legacy entity articles remain readable but are no longer scheduled | When cluster discovery, lineage, synthesis, or serving behavior changes |
| `docs/adr/ADR-0008-operator-conflict-resolution.md` | Makes conflict flags decidable: unresolved `conflict_flag`s become proposals in the non-substrate `proposed_conflict_resolutions` queue (enqueued by the resolver + a bounded backfill over historical flags), decided ONLY through `decide_correction` via `cfl_` prefix dispatch (the ADR-0003 `ont_` precedent), reachable from both `recall(decide=)` and the dashboard decide route. Three operator intents (keep newer / not a conflict / keep older) map onto the frozen confirm/reject/retract enum via directional framing, no wire change; resolution is append-only (resolution interpretation + `invalidate` event + observe; sources and flags never mutated, I2); resolved flags are served WITH their resolution (ADR-0004 posture) but excluded from unresolved counts | When the conflict-resolution loop or its serving changes |
| `CLAUDE.md` (this file) | Project-specific working rules + current state + phase status | After each merge that changes state |
| `README.md` | Public-facing setup + orientation; the two-paths (self-host vs hosted afair.ai) front door | When setup steps change |
| `LICENSE` | AGPLv3: the open-source core license (see VISION §12) | Only on a license change |
| `CONTRIBUTING.md` | Contributor setup, the four checks, the invariants a change can't break | When dev workflow changes |
| `CODE_OF_CONDUCT.md` | Contributor Covenant 2.1 (verbatim); contact hello@afair.ai | Rare |
| `SECURITY.md` | How to report vulnerabilities + the security model to hold afair against | When the security model changes |
| `CHANGELOG.md` | Keep-a-Changelog history; `[Unreleased]` until first tag | Per notable user-facing change |
| `CITATION.cff` | How to cite afair (research-grounded project) | On release / author change |
| `.github/ISSUE_TEMPLATE/*` + `PULL_REQUEST_TEMPLATE.md` | Structured bug/feature/PR forms | When the contribution flow changes |
| `.env.example` | Full env-var reference, with hosted variables marked optional | When env shape changes |
| `docs/self-hosting.md` | Self-host guide: local + prod, encryption + the vault key, backups, upgrades | When setup/encryption changes |
| `docs/clients/*.md` | Per-client MCP connection config + universal instruction snippet | When client integration changes |
| `.github/workflows/ci.yml` | Product CI: lint/type/test on push + PR. No deploy, no secrets | When the gate set changes |
| `.github/workflows/release.yml` | A `vX.Y.Z` tag publishes a GitHub Release (notes from the CHANGELOG section) and dispatches the afair-web fleet deploy at that tag (manual-tag fallback path) | When the release flow changes |
| `.github/workflows/release-please.yml` + `release-please-config.json` + `.release-please-manifest.json` | Default automated release flow: a standing release PR bumps the version + CHANGELOG from Conventional Commits; merging it tags, publishes the GitHub Release, and deploys the fleet (uv.lock synced on the PR) | When the release flow changes |
| `tests/goldens/mcp_surface.json` | Golden snapshot of the advertised WIRE MCP surface (fastmcp in-memory `Client` view: tool in/out schemas, descriptions inline, instructions as sha256, session-start resource). Locks the client-facing contract so a v0.1.9-class schema regression (a `\| str` widening leaking a top-level `anyOf:[obj,string]` that broke claude.ai tool surfacing while all tests stayed green) fails as a red diff. `tests/test_mcp_surface.py` also lints every param: top-level object, no bare-string-union pollution, draft-2020-12 validity. I1: a golden diff must be additive; a removal/rename/tightening is an I1 violation | When the MCP surface intentionally changes (regen via the dump script) |
| `scripts/dump_mcp_surface.py` | Regenerate `tests/goldens/mcp_surface.json` from a temp-vault server. Run only when a client-facing surface change is intended, then review the golden `git diff` | When the surface capture/normalization changes |
| `scripts/smoke.sh` | Curl-only health + auth gate smoke (no Python) | Rare: when transport changes |
| `scripts/smoke_mcp.py` | Full MCP-protocol round-trip smoke against live server | When tool contract changes |
| `scripts/backfill_entities.py` | One-shot entity-graph backfill (Phase 4 Track 1 rebuild path) | Rare: when canonicalizer interface changes |
| `scripts/checkup_entities.py` | Read-only entity-graph checkup (ADR-0003 Phase 2 verification): identity/cluster/formation/drain census + `other`-wildcard metric | When the Phase 2 diagnostics change |
| `scripts/drain_entity_dedup.py` | Supervised operator drain of the same-name cluster backlog (loops the deduplicator at a raised cap; `--dry-run`/`--max-clusters`/`--sleep`) | When the deduplicator interface changes |
| `scripts/install_clients.py` | One-command MCP client installer (writes config + snippet) | When client integration changes |
| `scripts/check_secrets.py` | Pre-deploy guard: verify a Fly app has the boot-required secrets (+ `--diff` parity). Run by the afair-web fleet deploy | When a new ENVIRONMENT=fly boot validator is added |
| `afair/substrate/provenance.py` + the `event_provenance` table | The ADR-0006 sidecar implementation: `INSERT OR IGNORE` stamps one row per distinct `(event_id, client)` (a second client on a dedup'd event appends an honest second row; stamping sits fail-soft OUTSIDE the `was_inserted` branch), batch reads ordered author-first, `by_client` distinct-event counts. Append-only (I2 triggers from day one); slug is credential-derived only, never headers/args | When the provenance model changes |
| `afair/mcp/host_canon.py` | Transport-only host canonicalization on managed vaults (v0.1.16): a vault reached on a non-canonical host gets a 308 to the canonical issuer for browser/discovery GETs and a `421 Misdirected Request` for `POST /mcp`; the token audience is never widened. No-op unless `environment=fly` with an explicit `oauth_issuer`; health + `/internal/*` pass through | When the canonical-host policy changes |
| `afair/agents/living_syntheses.py` | Deterministic emergent cluster discovery followed by model-written, cited synthesis. Combines entity recurrence, strong Binder links, and explicit lineage; suppresses mature hubs; preserves cluster identity and split/merge ancestry; replaces entity articles in the scheduler without breaking legacy reads | When automatic cluster discovery or synthesis changes |
| `afair/eval/memory_quality.py` + `afair/eval/fixtures/memory_quality.jsonl` | End-to-end product-quality gate over final answers and syntheses: truth, current-state recall, stale exclusion, citation coverage and validity, conflict honesty, abstention, and cross-tool consistency. Public fixture is deterministic; private vault replay emits the same shape | When the memory-quality scorecard or gates change |
| `afair/mcp/memory_mirror_route.py` | Dashboard-authenticated read-only projection of live syntheses, sources, stale evidence, and unresolved conflicts. Reads the user's own vault, stores nothing, and exposes no new MCP tool | When the Memory Mirror projection changes |
| `afair/mcp/import_route.py` | Dashboard-authenticated, user-initiated normalized import. Writes ordinary append-only remember events from ChatGPT, Claude, Obsidian, Notion, or files, then schedules normal extraction. Content travels directly to the single-tenant vault | When supported import shapes or limits change |
| `afair/agents/extraction_retry.py` | Bounded cold-path retry of TRANSIENT extraction failures (`llm_timeout` / `llm_rate_limit`); deterministic failures are never retried. A retry appends a NEW interpretation row (I2; the failure stays as audit trail) and the attempt count is derived from the failed rows, not a mutable counter | When the retry policy / failure taxonomy changes |
| `afair/substrate/watermarks.py` + the `worker_watermarks` table | Mutable per-worker re-scan cursors (P2a): a worker advances only after a fully-drained zero-failure cycle, to a LAGGED frontier so a concurrently pre-minted id can never be stranded (never-skip contract). Non-substrate, no I2 triggers (the `proposed_corrections` / `export_jobs` framing); deleting a row just re-scans once, lossless. Also hosts the `edge_serves_epoch` marker that anchors edge auto-expiry grace | When a worker's cursor contract changes |
| the `edge_serves` table (`substrate/schema.py`; written from recall's entity overlay) | Append-only "this edge was actually served in a recall" signal (I2 triggers; on the Pruner never-touch list; a durable gate input, NOT telemetry). Gates the review queue (only served edges are proposed) and the auto-expiry of never-served low-confidence edges, whose grace anchors to the serve-tracking epoch so a deploy can't mass-retire the legacy graph | When the serve-gating / expiry policy changes |
| _(hosted fleet ops: `provision`/`retire`/`hourly-backup` workflows + `provision_user.py`/`retire_user.py`/`recover_user.py`/`onboarding_email.py`)_ | Live only in the private **afair-web** repo (control plane); scrubbed from this repo's history | n/a here |
| `AGENTS.md` | Thin pointer file at repo root for non-Claude AI assistants (Codex CLI, Cursor) that look for AGENTS.md by convention: redirects to CLAUDE.md as canonical | When the read-order changes |
| `assets/logo/` | Brand assets: primary logo (`afair-elephant.png`), inverse (dark mode), SVG trace, favicon set, GitHub social preview. Regeneration recipe in the afair-web operator runbook | When the source logo changes |

## 5. Invariants: quick reference

(Full text in VISION.md §4, these are summaries for lookup, not authoritative.)

- **I1**: MCP tools are versioned and additive. Shipped signatures never break.
- **I2**: Substrate is append-only, content-addressed. Protects the user's *memory* (events, interpretations, entities, edges, temporal/belief metadata). `interpretations` is trigger-enforced: no updates ever, and no deletes except rows produced by `extractor:%` (the Pruner's GC of regenerable stale-failed extractions), so an ADR-0008 `conflict_resolution:v1:` decision-of-record can never be deleted. NOT protected: purely operational tables (`proposed_corrections`, `proposed_conflict_resolutions`, `export_jobs`, `worker_watermarks`, and the telemetry flight recorder `pipeline_events` / `observability_snapshots`), which are non-substrate, carry no I2 triggers, and are prunable or mutable (ADR-0005).
- **I3**: Old data must remain readable, queryable, re-interpretable. Migrations are forbidden; new views over unchanged substrate are required.
- **I4**: User owns the substrate. Self-hosting is first-class.
- **I5**: No code path privileges one AI provider. litellm wrapper, env-driven model selection.
- **I6**: No fixed ontology. Extractor uses context cues, not a hardcoded enum of types.
- **I7**: Self-modification is recorded and reversible. I1 to I6 are exempt from self-modification.
- **I8**: Single-tenant. No shared DB, no shared app server. Per-user dedicated Fly machine in managed.

## 6. Path-scoped rules

None yet. Add `.claude/rules/<topic>.md` (with `paths:` frontmatter) if a
subsystem grows conventions worth scoping; none have so far.

## 7. Quick commands

```bash
# install deps
uv sync

# run server locally
uv run python -m afair

# run tests
uv run pytest

# type check
uv run mypy afair

# lint
uv run ruff check
uv run ruff format

# build docker image
docker build -t afair .

# deploy the fleet: from afair-web, NOT here (tag a release, or dispatch):
gh workflow run deploy-afair-fleet.yml -R afairai/afair-web -f target=prod -f ref=main
```
