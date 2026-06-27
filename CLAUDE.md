# Project memory — afair

> Read `VISION.md` first. It is the operating constitution. The invariants in §4 are inviolable.
> This file is for project-specific working rules that complement (never override) the constitution.

## 0. Current state

**Phase:** 0 — Substrate + MCP Surface (cross-vendor, zero automated ingestion)
**Status:** Capability gate PASSED 2026-06-14 (see `analysis/phase-0-journal.md`);
Phase 1 next. The substrate, frozen 3-verb MCP surface, cold-path agents,
entity graph, recall honesty layer, and recursive self-improvement loop are
all live and in daily real-world use.
**Audience:** solo build; future contributors

### 0.1 What's live

All of this is in daily real-world use. Per-feature detail + the dated
build-log live in [`analysis/build-log.md`](analysis/build-log.md).

- **Substrate** (`afair/substrate/`) — append-only, content-addressed SQLite +
  FTS5 + sqlite-vec, encrypted at rest (SQLCipher + AES-GCM); I2 enforced by DB
  triggers.
- **MCP surface** (`afair/mcp/`) — the three frozen v1 verbs
  (remember / recall / observe) over Streamable HTTP, OAuth 2.1 + scoped-bearer
  auth, multi-modal content (text/PDF/audio/image), streaming blob upload,
  async vault export.
- **Cold-path agents** (`afair/agents/`) — extractor, salience, mode-switcher,
  surprise, entity canonicalizer + emergent entity graph (5 substrate tables),
  conflict resolver, consolidator, and the recursive self-improvement loop
  (tuner + multi-vendor judge + rollback monitor).
- **Hosted layer** — per-user single-tenant Fly machines (I8), provisioned and
  retired from afair-web. Each user gets a per-user vanity host
  (`<name-suffix>.mcp.afair.ai`, suffix derived from their Clerk id). The
  operator's own vault is the per-user app `afair-solis-e03` at
  `solis-e03.mcp.afair.ai` (since 2026-06-14; the old single-tenant
  `mcp.afair.ai` app was retired). There is no bare `mcp.afair.ai` vault.
- **Deploy** — product CI in this repo (`.github/workflows/ci.yml`: lint/type/
  test, no deploy). The fleet deploys from the private **afair-web** repo
  (`deploy-afair-fleet.yml`, pinned ref); a `vX.Y.Z` tag here triggers it. The
  operator runbook lives in afair-web; self-host docs are `docs/self-hosting.md`.

### 0.2 In flight / recent

- **Open-core split + going public** — repos moved to the `afairai` org (still
  private); deploy split (fleet from afair-web); OSS community-health files
  (CONTRIBUTING / SECURITY / CoC / CHANGELOG / CITATION / templates);
  history scrubbed of the private strategy docs + the operator Clerk ID. The
  remaining steps live in `afair-web/strategy/going-public-checklist.md`.

### 0.3 Blocked

- Nothing.

### 0.4 Captured for later (not active build)

- **Vault Dashboard** — read-only insight surface on the control plane
  (entity-graph hero, surprise heatmap). After the daily-use window.
- **Observability (Phase 0.5)** — pipeline_events lifecycle tracing +
  expectation checker + enriched `/health`. Design in
  `analysis/2026-05-28-observability-strategy.md`.
- **Early-access signup professionalization** — at ≥50–100 signups: dedicated
  store, double opt-in, admin broadcast.
- **Funding stance** — bootstrap-default / VC-conditional (private note).
## 1. Naming (post-rebrand)

`afair` is the **final product name**. `afair.ai` is registered. Code,
repo, MCP server, Fly apps, Docker image all use it. The earlier
codename phase is over.

Working rule for code: keep imports relative (`from . import substrate`)
rather than absolute (`from afair.substrate import ...`) so the package
directory could be renamed later without churn — but no rename is
planned.

## 2. Stack

| Concern | Choice | Reason |
|---|---|---|
| Language | Python 3.12+ | Memory/agent research ecosystem is Python; FastMCP is Python-first |
| Package manager | `uv` | Fast, modern, lockfile-stable |
| MCP framework | FastMCP | Most mature Python MCP server |
| MCP transport | Streamable HTTP only | Required for Claude.ai connectivity; works equally well on localhost |
| HTTP layer | FastAPI (under FastMCP) | Non-MCP endpoints (health) |
| Storage | SQLite + FTS5 + sqlite-vec | Single file, transactional, portable, inspectable |
| LLM | litellm wrapper, default `anthropic/claude-haiku-4-5` | I5 vendor neutrality from day one |
| Replication (managed) | LiteFS | Deferred to Phase 8 |
| Validation | Pydantic v2 | At every boundary, parse-don't-cast |
| Logging | structlog | JSON with PII redaction |
| Deployment | Docker → Fly machine | Single binary across local / self-hosted / managed |

## 2.5 Public-repo discipline (binding)

The open-source core (this `afair` repo) is **AGPLv3** (see `LICENSE` and
`VISION.md` §12). The hosted control plane (`afair-web`: provisioning,
billing, dashboard) is a **separate private repo** and never merges here.
The operational rule stands:

> **Build as if the repo is public — because the core is.**

Concretely:

- No secrets, credentials, tokens, or API keys in committed files. Use
  `.env.local` (gitignored) for local secrets; keep durable backups of any
  keys outside the repo.
- No internal jokes, no slurs against competitors, no offhand snark
  in code, comments, or commit messages.
- No proprietary algorithmic tricks that depend on staying secret for
  their value. The moat is single-tenant + cross-vendor + brand —
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
- Commit messages reviewable by a stranger — conventional commits,
  imperative voice, no "fix the thing" subjects.
- Transitive dependencies stay AGPL-compatible (Apache2/MIT/BSD). No
  incompatible-copyleft deps that would conflict with the AGPLv3 release.

Anything genuinely business-internal (funding strategy, competitor
teardowns, marketing positioning, hosted-control-plane design) lives in
gitignored local files or in the private `afair-web` repo — never in this
one. See the `.gitignore` "Business-internal strategy docs" block.

## 3. Trust ladder (binding for all phases)

1. **Phase 0** — explicit `remember` / `observe` calls only. Zero automated ingestion.
2. **Phase 1+** — user-initiated manual import (paste, drag, "ingest this URL").
3. **Phase 2+** — opt-in connectors, one source at a time, salience filter auditable before enable.
4. **Continuous sensing** (Gmail, Calendar, Slack, Drive) — earned, not assumed at install.

If a feature proposal requires accessing user data the user hasn't deliberately handed in, the answer is no until the trust ladder reaches that rung.

## 4. Documentation registry

| File | Purpose | Update cadence |
|---|---|---|
| `VISION.md` | The constitution — vision, mission, invariants, architecture, competitive landscape, research grounding | Quarterly review; treat as zeitlos — no per-phase status updates |
| `docs/adr/ADR-0001-constitutional-invariants.md` | Why the eight invariants exist + why drawn this way: each negates a failure mode, the three reinforcing chains, the I2-erasure clarification, and the accepted bets (I8 economics, I3 projection discipline, I7 aspirational). Has re-examination triggers | When an invariant is re-examined or a bet resolves |
| `docs/adr/ADR-0002-belief-revision-derived-layer.md` | Treat the entity graph as defeasible beliefs: entrenchment trust tiers (AGM), source-cascade retraction (JTMS/defeasible), evidence-grounding (= the relation fix), quarantine + auto-confirm policy (KG HITL), correction-on-recall (reconsolidation). Grounded in cited papers. Drove the `edge_reviews` table + `substrate/belief.py` | When the trust model / confirm-loop changes |
| `CLAUDE.md` (this file) | Project-specific working rules + current state + phase status | After each merge that changes state |
| `README.md` | Public-facing setup + orientation; the two-paths (self-host vs hosted afair.ai) front door | When setup steps change |
| `LICENSE` | AGPLv3 — the open-source core license (see VISION §12) | Only on a license change |
| `CONTRIBUTING.md` | Contributor setup, the four checks, the invariants a change can't break | When dev workflow changes |
| `CODE_OF_CONDUCT.md` | Contributor Covenant 2.1 (verbatim); contact hello@afair.ai | Rare |
| `SECURITY.md` | How to report vulnerabilities + the security model to hold afair against | When the security model changes |
| `CHANGELOG.md` | Keep-a-Changelog history; `[Unreleased]` until first tag | Per notable user-facing change |
| `CITATION.cff` | How to cite afair (research-grounded project) | On release / author change |
| `.github/ISSUE_TEMPLATE/*` + `PULL_REQUEST_TEMPLATE.md` | Structured bug/feature/PR forms | When the contribution flow changes |
| `.env.example` | Full env-var reference (all 34 settings, hosted vars marked optional) | When env shape changes |
| `docs/self-hosting.md` | Self-host guide: local + prod, encryption + the vault key, backups, upgrades | When setup/encryption changes |
| `docs/clients/*.md` | Per-client MCP connection config + universal instruction snippet | When client integration changes |
| `.github/workflows/ci.yml` | Product CI — lint/type/test on push + PR. No deploy, no secrets | When the gate set changes |
| `.github/workflows/release.yml` | A `vX.Y.Z` tag publishes a GitHub Release (notes from the CHANGELOG section) and dispatches the afair-web fleet deploy at that tag (manual-tag fallback path) | When the release flow changes |
| `.github/workflows/release-please.yml` + `release-please-config.json` + `.release-please-manifest.json` | Default automated release flow: a standing release PR bumps the version + CHANGELOG from Conventional Commits; merging it tags, publishes the GitHub Release, and deploys the fleet (uv.lock synced on the PR) | When the release flow changes |
| `scripts/smoke.sh` | Curl-only health + auth gate smoke (no Python) | Rare — when transport changes |
| `scripts/smoke_mcp.py` | Full MCP-protocol round-trip smoke against live server | When tool contract changes |
| `scripts/backfill_entities.py` | One-shot entity-graph backfill (Phase 4 Track 1 rebuild path) | Rare — when canonicalizer interface changes |
| `scripts/install_clients.py` | One-command MCP client installer (writes config + snippet) | When client integration changes |
| `scripts/check_secrets.py` | Pre-deploy guard: verify a Fly app has the boot-required secrets (+ `--diff` parity). Run by the afair-web fleet deploy | When a new ENVIRONMENT=fly boot validator is added |
| _(hosted fleet ops — `provision`/`retire`/`hourly-backup` workflows + `provision_user.py`/`retire_user.py`)_ | Moved to the private **afair-web** repo (control plane), not in this product repo | n/a here |
| `analysis/build-log.md` | Archived per-feature build-log (detail moved out of CLAUDE.md §0 to keep it lean) | Append when a phase closes |
| `analysis/phase-0-journal.md` | Daily-use log for the Phase 0 capability gate | Daily during the two-week window |
| `analysis/2026-06-03-recursive-self-improvement.md` | Design of the tuner / judge / rollback self-improvement loop (referenced from `afair/agents/tuner.py`) | When the loop design changes |
| `analysis/2026-05-28-observability-strategy.md` | Three-layer plan to make the designed flow visible — pipeline_events table, expectation checker, enriched /health. Triggered by heizzeit stall + consolidator silence | Refresh as drops 1–7 ship |
| `analysis/2026-06-27-memory-relevance-decay-spec.md` | Spec for time/relevance-aware recall: eight relevance classes (dated, recurring, superseded, decaying, transient, evergreen, periodic, commitment), salience decay + re-surfacing within I2/I3 (decay is a recall score, never a delete) | When the relevance/decay design changes |
| `AGENTS.md` | Thin pointer file at repo root for non-Claude AI assistants (Codex CLI, Cursor) that look for AGENTS.md by convention — redirects to CLAUDE.md as canonical | When the read-order changes |
| `assets/logo/` | Brand assets — primary logo (`afair-elephant.png`), inverse (dark mode), SVG trace, favicon set, GitHub social preview. Regeneration recipe in the afair-web operator runbook | When the source logo changes |

## 5. Invariants — quick reference

(Full text in VISION.md §4 — these are summaries for lookup, not authoritative.)

- **I1** — MCP tools are versioned and additive. Shipped signatures never break.
- **I2** — Substrate is append-only, content-addressed.
- **I3** — Old data must remain readable, queryable, re-interpretable. Migrations are forbidden; new views over unchanged substrate are required.
- **I4** — User owns the substrate. Self-hosting is first-class.
- **I5** — No code path privileges one AI provider. litellm wrapper, env-driven model selection.
- **I6** — No fixed ontology. Extractor uses context cues, not a hardcoded enum of types.
- **I7** — Self-modification is recorded and reversible. I1–I6 are exempt from self-modification.
- **I8** — Single-tenant. No shared DB, no shared app server. Per-user dedicated Fly machine in managed.

## 6. Path-scoped rules (when added)

- `afair/substrate/` — see `.claude/rules/substrate.md` (forthcoming, task #2)
- `afair/mcp/` — see `.claude/rules/mcp.md` (forthcoming, task #3)
- `afair/agents/` — see `.claude/rules/agents.md` (forthcoming, task #4)

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

# deploy the fleet — from afair-web, NOT here (tag a release, or dispatch):
gh workflow run deploy-afair-fleet.yml -R afairai/afair-web -f target=prod -f ref=main
```
