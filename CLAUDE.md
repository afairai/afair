# Project memory: afair

> Read `VISION.md` first. It is the operating constitution. The invariants in §4 are inviolable.
> This file is for project-specific working rules that complement (never override) the constitution.

## 0. Current state

**Status:** In daily real-world use. The substrate, the frozen 3-verb MCP
surface, the cold-path agents (including the relevance-decay/temporal worker),
the entity graph, the recall honesty layer, and the recursive self-improvement
loop are all live. Current focus: the open-source launch.
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
  async vault export.
- **Cold-path agents** (`afair/agents/`): extractor, salience, mode-switcher,
  surprise, temporal (relevance-decay), entity canonicalizer + emergent entity
  graph (5 substrate tables), schema evolver (emergent ontology, ADR-0003:
  proposes kind revisions from usage; the operator confirms/rejects/reverts
  through `recall(decide=)`), conflict resolver, consolidator, and the
  recursive self-improvement loop (tuner + multi-vendor judge + rollback
  monitor).
- **Hosted layer**: per-user single-tenant Fly machines (I8), provisioned and
  retired from afair-web. Each user gets a per-user vanity host
  (`<name-suffix>.mcp.afair.ai`, suffix derived from their Clerk id). There is
  no bare `mcp.afair.ai` vault; every vault is its own per-user app.
- **Deploy**: product CI in this repo (`.github/workflows/ci.yml`: lint/type/
  test, no deploy). The fleet deploys from the private **afair-web** repo
  (`deploy-afair-fleet.yml`, pinned ref); a `vX.Y.Z` tag here triggers it. The
  operator runbook lives in afair-web; self-host docs are `docs/self-hosting.md`.

### 0.2 In flight / recent

- **ADR-0004 edge-confidence model (branch, all 8 slices green).** The flat 0.8
  edge confidence is replaced by a transparent log-odds model: a write-time
  prior in the `entity_edges.confidence` column plus an append-only
  `edge_confidence_scores` overlay (latest-wins, column fallback). A cold-path
  `edge_scorer` backfills the legacy rows (never mutated, I2/I3) and re-scores on
  new corroboration / a contested source. Consumers wired: a discriminating
  auto-confirm floor, per-edge served `confidence` + a low-confidence caveat in
  recall (served WITH a caveat, not suppressed — operator-decided fork), the
  `edge_review` proposal queue (giving `record_edge_review` its first production
  caller), the article-synth filter, and three bounded tuner tunables with
  `calibration_report` as the promote evidence (`promote_enabled` still False).
  Not yet merged / not applied to any live vault.
- **ADR-0003 Phase 2 made effective on the live vault.** The kind-decoupling
  (v2 identities, mutable kinds) shipped in v0.1.5; a six-slice completion pass
  (ADR-0003 now `Accepted`) closes the remaining gaps: a read-only checkup
  (`scripts/checkup_entities.py`), the canonicalizer defers on LLM-budget
  exhaustion instead of minting cross-kind duplicates, the deduplicator unifies
  kinds via assignment at high confidence (no review flood) and skips recorded
  homonym splits, and a supervised drain tool (`scripts/drain_entity_dedup.py`)
  works the v1 backlog down. Drain runs against the live vault are a later
  supervised operator step (self-host runbook: `docs/self-hosting.md`).
- **Going public (open-core).** Repos live in the `afairai` org; the deploy is
  split (the fleet ships from the private afair-web repo); OSS community-health
  files are in place; operator/fleet tooling lives only in afair-web. The git
  history was scrubbed of that tooling (provision/retire, fly configs, deploy
  workflows). No secrets were ever in history, and the business-confidential
  docs were never committed (they have always been gitignored). `main`, the
  tags, and the release-please branch are clean on GitHub; the stale `dev`
  branch was deleted. Final pre-flip pass: internal and personal material
  moved to afair-web (the `analysis/` notes, the operator vault host), with a
  second history scrub for it. Then the visibility flip,
  `gh repo edit afairai/afair --visibility public`.
- **scripts/ hygiene.** Only self-hoster and contributor scripts remain; fleet
  tooling moved to afair-web; `bench.py` defaults to the local server.

### 0.3 Blocked

- Nothing.

### 0.4 Captured for later (not active build)

- **Vault Dashboard**: read-only insight surface on the control plane
  (entity-graph hero, surprise heatmap). After the daily-use window.
- **Observability (Phase 0.5)**: pipeline_events lifecycle tracing +
  expectation checker + enriched `/health`.
- **Early-access signup professionalization**: at ≥50–100 signups: dedicated
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
| `VISION.md` | The constitution: vision, mission, invariants, architecture, competitive landscape, research grounding | Quarterly review; treat as zeitlos, no per-phase status updates |
| `docs/adr/ADR-0001-constitutional-invariants.md` | Why the eight invariants exist + why drawn this way: each negates a failure mode, the three reinforcing chains, the I2-erasure clarification, and the accepted bets (I8 economics, I3 projection discipline, I7 aspirational). Has re-examination triggers | When an invariant is re-examined or a bet resolves |
| `docs/adr/ADR-0002-belief-revision-derived-layer.md` | Treat the entity graph as defeasible beliefs: entrenchment trust tiers (AGM), source-cascade retraction (JTMS/defeasible), evidence-grounding (= the relation fix), quarantine + auto-confirm policy (KG HITL), correction-on-recall (reconsolidation). Grounded in cited papers. Drove the `edge_reviews` table + `substrate/belief.py` | When the trust model / confirm-loop changes |
| `docs/adr/ADR-0003-emergent-ontology.md` | Proposed design that discharges the I6 debt: entity kinds become an append-only registry (add/rename/merge/split/deprecate revisions), kind decouples from entity identity (v2 IDs + kind-assignment overlay + resolution views, no migration), and the VISION §6.5 Schema-Evolver ships as a propose-only cold-path worker gated by the ADR-0002 `recall(decide=)` confirm loop | When the ontology model / Schema-Evolver design changes |
| `docs/adr/ADR-0004-edge-confidence-model.md` | Replaces the flat 0.8 edge confidence with a transparent log-odds model (write-time prior in the `confidence` column + append-only `edge_confidence_scores` overlay, latest-wins with column fallback). Wires the consumers: discriminating auto-confirm floor, per-edge served `confidence` + low-confidence caveat in recall, the `edge_review` proposal queue (first production caller of `record_edge_review`), the article-synthesizer filter, and three bounded tuner tunables with `calibration_report` as the evidence. Legacy edges never mutated (I2/I3); scored by the cold-path `edge_scorer`. Low-confidence edges served with a caveat, not suppressed | When the confidence model / its consumers change |
| `CLAUDE.md` (this file) | Project-specific working rules + current state + phase status | After each merge that changes state |
| `README.md` | Public-facing setup + orientation; the two-paths (self-host vs hosted afair.ai) front door | When setup steps change |
| `LICENSE` | AGPLv3: the open-source core license (see VISION §12) | Only on a license change |
| `CONTRIBUTING.md` | Contributor setup, the four checks, the invariants a change can't break | When dev workflow changes |
| `CODE_OF_CONDUCT.md` | Contributor Covenant 2.1 (verbatim); contact hello@afair.ai | Rare |
| `SECURITY.md` | How to report vulnerabilities + the security model to hold afair against | When the security model changes |
| `CHANGELOG.md` | Keep-a-Changelog history; `[Unreleased]` until first tag | Per notable user-facing change |
| `CITATION.cff` | How to cite afair (research-grounded project) | On release / author change |
| `.github/ISSUE_TEMPLATE/*` + `PULL_REQUEST_TEMPLATE.md` | Structured bug/feature/PR forms | When the contribution flow changes |
| `.env.example` | Full env-var reference (all 34 settings, hosted vars marked optional) | When env shape changes |
| `docs/self-hosting.md` | Self-host guide: local + prod, encryption + the vault key, backups, upgrades | When setup/encryption changes |
| `docs/clients/*.md` | Per-client MCP connection config + universal instruction snippet | When client integration changes |
| `.github/workflows/ci.yml` | Product CI: lint/type/test on push + PR. No deploy, no secrets | When the gate set changes |
| `.github/workflows/release.yml` | A `vX.Y.Z` tag publishes a GitHub Release (notes from the CHANGELOG section) and dispatches the afair-web fleet deploy at that tag (manual-tag fallback path) | When the release flow changes |
| `.github/workflows/release-please.yml` + `release-please-config.json` + `.release-please-manifest.json` | Default automated release flow: a standing release PR bumps the version + CHANGELOG from Conventional Commits; merging it tags, publishes the GitHub Release, and deploys the fleet (uv.lock synced on the PR) | When the release flow changes |
| `scripts/smoke.sh` | Curl-only health + auth gate smoke (no Python) | Rare: when transport changes |
| `scripts/smoke_mcp.py` | Full MCP-protocol round-trip smoke against live server | When tool contract changes |
| `scripts/backfill_entities.py` | One-shot entity-graph backfill (Phase 4 Track 1 rebuild path) | Rare: when canonicalizer interface changes |
| `scripts/checkup_entities.py` | Read-only entity-graph checkup (ADR-0003 Phase 2 verification): identity/cluster/formation/drain census + `other`-wildcard metric | When the Phase 2 diagnostics change |
| `scripts/drain_entity_dedup.py` | Supervised operator drain of the same-name cluster backlog (loops the deduplicator at a raised cap; `--dry-run`/`--max-clusters`/`--sleep`) | When the deduplicator interface changes |
| `scripts/install_clients.py` | One-command MCP client installer (writes config + snippet) | When client integration changes |
| `scripts/check_secrets.py` | Pre-deploy guard: verify a Fly app has the boot-required secrets (+ `--diff` parity). Run by the afair-web fleet deploy | When a new ENVIRONMENT=fly boot validator is added |
| _(hosted fleet ops: `provision`/`retire`/`hourly-backup` workflows + `provision_user.py`/`retire_user.py`/`recover_user.py`/`onboarding_email.py`)_ | Live only in the private **afair-web** repo (control plane); scrubbed from this repo's history | n/a here |
| `AGENTS.md` | Thin pointer file at repo root for non-Claude AI assistants (Codex CLI, Cursor) that look for AGENTS.md by convention: redirects to CLAUDE.md as canonical | When the read-order changes |
| `assets/logo/` | Brand assets: primary logo (`afair-elephant.png`), inverse (dark mode), SVG trace, favicon set, GitHub social preview. Regeneration recipe in the afair-web operator runbook | When the source logo changes |

## 5. Invariants: quick reference

(Full text in VISION.md §4, these are summaries for lookup, not authoritative.)

- **I1**: MCP tools are versioned and additive. Shipped signatures never break.
- **I2**: Substrate is append-only, content-addressed.
- **I3**: Old data must remain readable, queryable, re-interpretable. Migrations are forbidden; new views over unchanged substrate are required.
- **I4**: User owns the substrate. Self-hosting is first-class.
- **I5**: No code path privileges one AI provider. litellm wrapper, env-driven model selection.
- **I6**: No fixed ontology. Extractor uses context cues, not a hardcoded enum of types.
- **I7**: Self-modification is recorded and reversible. I1–I6 are exempt from self-modification.
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
