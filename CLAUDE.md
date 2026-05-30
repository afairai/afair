# Project memory — afair

> Read `VISION.md` first. It is the operating constitution. The invariants in §4 are inviolable.
> This file is for project-specific working rules that complement (never override) the constitution.

## 0. Current state

**Phase:** 0 — Substrate + MCP Surface (cross-vendor, zero automated ingestion)
**Status:** In execution
**Audience:** solo build; future contributors

### 0.1 What's live

- VISION.md (the constitution)
- Repo scaffold: pyproject.toml, Dockerfile, fly.toml, .env templates, CLAUDE.md
- Substrate layer (`afair/substrate/`) — append-only SQLite + FTS5 +
  filesystem object store; events table is STRICT-mode with UPDATE/DELETE
  triggers enforcing I2 at the DB level
- MCP server (`afair/mcp/`) — three v1 tools (remember/recall/observe)
  over Streamable HTTP, surface frozen 2026-05-26. `recall` is the single
  retrieval verb (survey, by-id, full-payload modes are kwargs);
  `remember(..., invalidates=[...])` supersedes prior facts. AI-facing
  tool descriptions, /health endpoint, binary-via-base64 + 10 MB cap in
  `remember`.
- Extractor agent (`afair/agents/`) — warm-path LLM extraction via
  litellm; default `anthropic/claude-haiku-4-5`; failed extractions stored
  as `status: failed` rows for retry/diagnosis
- **Phase 4 Track 1 Emergent Entity Graph** — five append-only substrate
  tables (`entities`, `entity_mentions`, `entity_edges`, `entity_merges`,
  `edge_invalidations`) materialized by the `EntityCanonicalizer` cold-path
  worker. Three-stage match (exact → LLM with Sonnet escalation → new),
  cascade-invalidation through edges, recall enrichment via
  `interpretation.canonical_entities` + `interpretation.entity_edges`,
  entity-aware query routing for `recall(query=)`. One-shot backfill at
  `scripts/backfill_entities.py`.
- **Phase 4 Track 2 v0 Surprise score** — per-hit
  `interpretation.surprise_score ∈ [0,1]` based on entity-novelty against
  the recent context window (last N events, default 20, configurable via
  `SURPRISE_CONTEXT_WINDOW`). Plus `surprise_components` audit dict.
  Mode-switching agent (Phase 2 dependency) still deferred.
- **Fly deployment live at https://afair.fly.dev** (MCP at `https://mcp.afair.ai`)
  — single-tenant machine in `fra`, 1 GB volume `vault` with **14-day
  auto-snapshots** (RPO ~24h, acceptable for Phase 0). Upgrade paths
  to <1s RPO documented in `docs/operations.md` §7 (hourly cron, LiteFS
  Cloud, Litestream — to be picked when invites force the question).
  `strategy = "immediate"`, `min_machines_running = 1`
- **GitHub Actions deploy pipeline** at `.github/workflows/deploy.yml` —
  branch-based on `main`, runs ruff + mypy + pytest gates, then
  `flyctl deploy --remote-only`, verifies `/health`
- `docs/operations.md` — runbooks for deploy, backup-to-laptop, snapshot
  restore, future RPO upgrade paths, permanent erasure, secret rotation
- **Post-launch hardening pass (2026-05-30 evening)** — all CRITICAL +
  IMPORTANT security and performance audit items closed; selected
  MINOR items too. Concretely:
  - **Security:** prompt-injection defenses across the four LLM
    cold-path workers (extractor, canonicalizer, conflict_resolver,
    consolidator) via a shared `untrusted.py` delimiter helper +
    structural defense against fabricated edges; scoped
    `/internal/signup` endpoint with its own bearer (web app no
    longer needs the full `AFAIR_AUTH_TOKEN`); bounded inputs on
    all MCP tool params (parent_hashes ≤ 50, context ≤ 4000 chars,
    observe extras ≤ 64 KB nested ≤ 200); per-IP rate limit + scheme
    allowlist on `/oauth/register` and `/oauth/revoke`; JWT-sub
    based rate-limit identity so a mint-and-rotate flood lands in
    one bucket; OAuth code/state hashed at rest; modern
    Permissions-Policy; CSP; `OAUTH_ISSUER` required in prod.
  - **Performance:** N+1 → 2 batched queries in `recall`;
    composite `events(kind, created_at DESC)` index;
    thread-local extractor connections; recursive-CTE
    `resolve_canonical_batch`; single-flight embedding cache to
    coalesce concurrent misses; whole middleware stack
    (`CorrelationIdMiddleware`, `SecurityHeadersMiddleware`,
    `BodySizeLimitMiddleware`, `BearerOrJwtMiddleware`,
    `RateLimitMiddleware`) rewritten from `BaseHTTPMiddleware` to
    pure ASGI; capped entity candidate pool at 5000 rows.
  - **Concurrency correctness:** WAL `busy_timeout` now precedes
    `journal_mode` so concurrent `open_db` calls wait the lock out
    instead of raising; OAuth code/state consume rewritten as
    atomic `DELETE … RETURNING` (RFC 6749 §4.1.2 one-shot now
    enforced under racing /oauth/token); `write_event` rewritten as
    `INSERT … ON CONFLICT(content_hash) DO NOTHING RETURNING`
    (idempotent under concurrent identical writes — the previous
    SELECT-then-INSERT TOCTOU surfaced as 500s); extractor's
    `submit` now tolerates atexit shutdown gracefully.
  - **Dead-code sweep:** removed `read_conflicts_for_event`,
    `_wait_for_pending`, `cleanup_expired_codes` — all zero
    callers, superseded by batch / scheduled-GC variants.
  Full session 2026-05-30 19:00–22:00 UTC; commits f3e161f..362b2cd.

### 0.2 What's in flight

- Task #6 — cross-vendor MCP verification (Claude Code, Codex CLI, Claude.ai)
- Task #7 — Phase 0 capability-gate journal (2-week daily-use window)
- Phase 4 Track 2 mode-switching agent (CEN↔DMN routing driven by cumulative surprise + auto-`observe()` on threshold) — depends on Phase 2 Salience agent which doesn't exist yet. v0 surprise-score per hit IS live.
- **Multi-user provisioning (`scripts/provision_user.py`)** — must exist
  before the first paying invite. Creates Fly machine + secrets, sets
  per-user backup strategy (default: inherit daily snapshots; opt-in:
  hourly cron, LiteFS Cloud namespace). Open architectural question:
  many-machines-one-app vs one-app-per-user. RPO upgrade paths in
  `docs/operations.md` §7.

### 0.3 What's blocked

- Nothing yet

### 0.4 Captured for later (not active build)

- **Vault Dashboard** — control-plane (`app.afair.ai`) read-only surface
  with entity-graph hero view ("brain cells connected" aesthetic via
  `react-force-graph-3d`), surprise heatmap, timeline scrubber,
  vault-stats strip. Design + framework selection captured in
  `analysis/2026-05-27-dashboard-concept.md`. Authorized by VISION.md
  §11 anti-pattern exception ("minimal management dashboard for the
  hosted offering is acceptable"). Build after daily-use window
  closes + marketing/control-plane bootstrap; uses real vault data,
  not synthetic.
- **Funding stance** — bootstrap by default, VC conditional, hard-reject
  any term sheet that pressures I1–I8. EU non-dilutive options
  (EIC Accelerator, EXIST, HTGF, Calm Company Fund, TinySeed, OSS Capital)
  explored before any traditional VC conversation. Re-evaluate at
  ≥500 paying users sustained for ≥90 days, or when a closing competitive
  window demands defense capital. Full reasoning, decision tree,
  reject criteria in `analysis/2026-05-27-funding-stance.md`.
- **Observability strategy (Phase 0.5)** — three layers to make the
  designed flow visible: (A) append-only `pipeline_events` table tracing
  every event's lifecycle from `extraction.enqueued` through
  `consolidation.included`; (B) declarative `ExpectationChecker` worker
  that catches silent no-shows ("extraction should complete within 120s",
  "consolidator should run daily for previous day if ≥3 events"); (C)
  enriched `/health` reporting per-worker liveness and expectation
  violations. Plus replace the in-process ThreadPoolExecutor with a
  SQLite-backed durable queue to eliminate restart-loss. Triggered by
  the 2026-05-28 heizzeit-event extraction stall + 2-day consolidator
  silence — both invisible to current tooling. Full design in
  `analysis/2026-05-28-observability-strategy.md`.
- **Early-access signup professionalization** — current `afair-web`
  signup flow (2026-05-28): Mailersend transactional send + dogfood
  `remember()` into the afair vault as source of truth. Works fine
  for pre-launch. **At ≥50–100 signups, professionalize:** dedicated
  SQLite-on-Fly-volume (or migrate to D1) as the operational store,
  proper unsubscribe + GDPR opt-in (double opt-in), admin dashboard
  on `app.afair.ai` for list export + broadcast composer, per-signup
  attribution (which Twitter thread / which podcast / etc.), bot/spam
  protection (Turnstile). Mailersend remains the send channel; storage
  separates from transactional.

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

## 2.5 Public-repo discipline (binding until VISION.md §12 resolves)

The repo is **not** open source today. The licensing decision is deferred
(see `VISION.md` §12 Licensing Posture). Until then the operational rule:

> **Build as if the repo goes public tomorrow.**

Concretely:

- No secrets, credentials, tokens, or API keys in committed files.
  Use `.env.local` (gitignored) + `.env.secrets.backup` (also gitignored,
  see global CLAUDE.md "Secrets-Backup-File Konvention").
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
- Transitive dependencies stay Apache2/MIT/BSD-compatible. Avoids
  accidentally tainting the codebase with copyleft we cannot undo
  if we later choose Apache2, or accidentally absorbing AGPL deps
  if we later choose closed.

This rule does NOT prejudge §16's licensing decision. It just ensures
we can EXECUTE either decision in Phase 6 without rewrites or churn.

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
| `CLAUDE.md` (this file) | Project-specific working rules + current state + phase status | After each merge that changes state |
| `README.md` | Public-facing setup + orientation | When setup steps change |
| `.env.example` | Required env vars with comments | When env shape changes |
| `.env.secrets.backup` | Canonical secrets backup (gitignored) | Whenever a secret is created/rotated |
| `docs/clients/*.md` | Per-client MCP connection config + universal instruction snippet | When client integration changes |
| `docs/operations.md` | Deploy, backup, restore, erasure runbooks | When ops procedures change |
| `.github/workflows/deploy.yml` | Branch-based CI deploy to Fly | When pipeline changes |
| `scripts/smoke.sh` | Curl-only health + auth gate smoke (no Python) | Rare — when transport changes |
| `scripts/smoke_mcp.py` | Full MCP-protocol round-trip smoke against live server | When tool contract changes |
| `scripts/backfill_entities.py` | One-shot entity-graph backfill (Phase 4 Track 1 rebuild path) | Rare — when canonicalizer interface changes |
| `scripts/install_clients.py` | One-command MCP client installer (writes config + snippet) | When client integration changes |
| `analysis/phase-0-journal.md` | Daily-use log for the Phase 0 capability gate | Daily during the two-week window |
| `analysis/2026-05-27-dashboard-concept.md` | Vault Dashboard design + React-framework selection (read-only insight surface on control plane; not active build) | Frozen — update only if architecture changes |
| `analysis/2026-05-27-funding-stance.md` | Bootstrap-default-VC-conditional funding decision + EU non-dilutive options + hard-reject criteria | Re-evaluate at ≥500 paying users or when a closing competitive window demands defense capital |
| `analysis/2026-05-28-observability-strategy.md` | Three-layer plan to make the designed flow visible — pipeline_events table, expectation checker, enriched /health. Triggered by heizzeit stall + consolidator silence | Refresh as drops 1–7 ship |
| `AGENTS.md` | Thin pointer file at repo root for non-Claude AI assistants (Codex CLI, Cursor) that look for AGENTS.md by convention — redirects to CLAUDE.md as canonical | When the read-order changes |
| `assets/logo/` | Brand assets — primary logo (`afair-elephant.png`), inverse (dark mode), SVG trace, favicon set, GitHub social preview. Regeneration recipe in `docs/operations.md §12` | When the source logo changes |

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

# deploy to Fly (after fly.toml is configured)
fly deploy
```
