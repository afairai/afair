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
- **Phase 2 Salience worker** (`afair/agents/salience.py`) — every 5 min
  scores unscored remember/observe events for salience ∈ [0, 1] using
  cheap substrate signals (entity_density, link_density, has_conflict,
  type_hint_bump, is_compound, recency). Stored as
  `interpretations.produced_by = "salience:v0"` with both the final
  score and per-component breakdown. Pure substrate-derived, no LLM
  call — scales linearly with event count.
- **Phase 2 Mode-switching agent** (`afair/agents/mode_switcher.py`)
  — every 2 min, reads the rolling 20-event salience sum and decides
  between CEN (focused, deliberate, cumulative ≥ 8.0) and DMN
  (wandering, integrative, cumulative ≤ 4.0). Two-threshold
  hysteresis prevents flapping. Transitions write a normal observe
  event (origin `agent:mode_switcher`) — current mode is recoverable
  from substrate at any time via `read_current_mode()`.
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
- **Multi-modal content + streaming uploads + compound events
  (2026-05-30 night)** — substrate now stores + recalls + extracts
  any modality. Concretely:
  - **PDF text extraction** via pypdf in the warm-path extractor
    (`afair/agents/binary_extractors.py`).
  - **Audio whisper transcription** via litellm
    (`openai/whisper-1` default, configurable).
  - **Image vision extraction** via vision-capable LLM with the
    same tool-use schema as the text path
    (`anthropic/claude-haiku-4-5` default).
  - **Streaming blob upload** at `POST /internal/blob/upload` —
    reads body chunk-by-chunk via `request.stream()` into a new
    `StreamingObjectWriter`. Peak RAM per request is the buffer
    (~1 MB) regardless of payload size; default cap raised from
    10 MB JSON-body to 1 GB streamed.
  - **BlobRefContent** discriminator on `RememberContent` references
    already-uploaded blobs by hash.
  - **CompoundContent** discriminator for atomic multi-part events
    (transcript + slides + screenshot as one event row with one
    content_hash and one recall hit).
  - **FTS enrichment after extraction** — when the extractor lands,
    it DELETE+INSERTs the events_fts row to include the extractor's
    summary + salient_facts + the extracted body. PDFs and audio
    become first-class FTS-searchable by content.
- **Phase 0.5 observability foundation (2026-05-30 night)** —
  append-only `pipeline_events` table + lifecycle markers at every
  key step (event.written, extraction.enqueued/started/completed,
  embedding.stored, …). Future ExpectationChecker worker will query
  this for stuck events. See `afair/substrate/pipeline_events.py`.
- **RPO upgraded from 24h to ~1h (2026-05-30)** — added
  `.github/workflows/hourly-backup.yml` running `flyctl volumes
  snapshots create` at :17 every hour. Operations.md §7 reflects the
  new baseline. Next upgrade rung is LiteFS Cloud when invites scale.
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
- **Recursive self-improvement foundation (2026-06-03)** — Phase A
  of `analysis/2026-06-03-recursive-self-improvement.md` shipped.
  Concretely:
  - `afair/agents/tunable_registry.py` — whitelist + spec + cache
    + bounded-delta validation. 6 tunables initially (salience
    weights, mode-switcher thresholds, surprise window, entity
    canonicalizer escalation, consolidator cutoff).
  - `afair/substrate/tuner_state.py` + new append-only DB table
    (CHECK + no-update/no-delete triggers). Records every promote,
    rollback, hypothesis, observation. Reads expose the active
    value per (worker, tunable).
  - `afair/agents/guards.py` — per-worker invariant suites (hard
    floor on variant quality).
  - `afair/agents/llm_judge.py` — multi-vendor judge panel via
    litellm (Anthropic + OpenAI + Google majority). Frozen prompt
    `JUDGE_PROMPT_VERSION=v0:2026-06-03`. Budget cap 200K tokens
    per cycle.
  - `afair/agents/replay.py` — re-run any worker on past events
    with two parameter sets, return matched pairs.
  - `afair/agents/tuner.py` — cold-path worker, registered in
    server.py with **`promote_enabled=False`** (Phase A observe-only).
    Traffic-triggered (≥ 50 new events or ≥ 24h).
  - Optional `feedback` arg on `recall()` (additive — I1-compliant).
    Empty payload no-op. Persists as tuner_state observation row.
  - Worker refactors: `salience`, `mode_switcher`, `surprise` read
    parameters from registry (with static defaults preserved).
  - Snippet additions in onboarding-email + session-start resource
    + `recall` tool description instruct AI clients to send feedback.
  - Tests: 47 new (`test_tunable_registry`, `test_recall_feedback`,
    `test_tuner_and_guards`). Full suite green.
- **Phase A hardening pass (2026-06-03)** — 8 audit findings closed
  before going live. Critical: evidence size cap (64 KB), defense-
  in-depth validation in `record_change`, untrusted-content wrapping
  in LLM-judge prompts, replay returns full structured output. High:
  public connection accessor, judge call timeout (30s), actual token
  tracking from litellm response, cross-tunable hysteresis invariant
  for mode_switcher. 14 new tests cover all 8 findings.
- **Phase B (2026-06-03 evening)** — promotion goes live with full
  safety stack:
  - LLM-judge integration: multi-vendor majority (Anthropic Sonnet +
    OpenAI GPT-5 + Gemini 2.5 Pro) via litellm, frozen prompt
    `v0:2026-06-03`, 70% threshold for promote, 200K tokens/cycle.
  - `Tuner(promote_enabled=True)` writes `kind='promote'` rows when
    judge majority + guards both pass. Pre-promote feedback baseline
    stashed in evidence for the rollback monitor.
  - `RollbackMonitor` new cold-path worker (5-min cadence) watches
    each promote until ≥ 50 events arrive, then compares useful-rate
    to baseline. Drop ≥ 10% → auto-rollback writing
    `kind='rollback'` with the restored old_value.
  - Cooldown per tunable after rollback (7 days lock).
  - Global halt: > 3 rollbacks in any 7-day window pauses ALL
    promotion (tuner still observes; humans clear the halt).
  - Hypothesis diversity: tuner rotates through the whitelist,
    alternating direction per tunable based on the most-recent
    hypothesis row.
  - Tests: 10 new in `test_phase_b.py` (mock judge → promote path,
    cooldown skip, halt condition, rollback fire on degradation,
    idempotent monitor). Full suite 490 green.

### 0.2 What's in flight

- _(nothing — the Phase 0 threads below are resolved)_

### 0.2-resolved (was in flight, now done)

- ~~Task #7 — Phase 0 capability-gate journal~~ → gate **PASSED 2026-06-14**.
  Met by sustained real-world daily use (the vault is full of genuine
  cross-vendor data); the architecture survived contact with reality
  without a rebuild and without breaking I1–I8. Verdict logged in
  `analysis/phase-0-journal.md`.
- ~~Task #6 — cross-vendor MCP verification~~ → server side verified live
  (OAuth 2.1 discovery, DCR, PKCE S256, RFC 9728, `/mcp` 401 challenge on
  `mcp.afair.ai`) and the authenticated 3-verb round-trip runs every
  session. Docs broadened to web vendors (`docs/clients/{chatgpt,
  perplexity}.md`, `claude-ai.md` rewritten for the now-shipped OAuth
  flow, reproducible "Server capabilities" section in the clients README).
  A one-time hands-on connect of the three web connectors is optional
  reach, not a gate condition.
- ~~Cumulative-surprise feed into ModeSwitcher~~ → live
  (`afair/agents/surprise.py`). The recall path already surfaced a
  per-hit surprise score; the per-event sibling (`read_recent_surprise`
  / `cumulative_surprise`, entity-novelty against a running
  within-window familiarity set, pure substrate, no LLM) now feeds the
  ModeSwitcher as an additive CEN trigger + DMN gate. Backward-compatible:
  with cumulative surprise 0 the decision reduces exactly to the old
  salience-only hysteresis. A novelty burst can shift attention to CEN
  before salience catches up, and high novelty blocks a premature drop
  to DMN. Thresholds static for now (`DEFAULT_SURPRISE_{CEN,DMN}_THRESHOLD`),
  promotable to tunables later. 6 new tests.
- ~~Phase 2 Salience agent + Phase 4 Track 2 mode-switching agent~~
  → both live as cold-path workers (see §0.1).
- ~~Multi-user provisioning script~~ → live at
  `scripts/provision_user.py`. App-per-user resolved the
  "many-machines-one-app vs one-app-per-user" question in favor of
  one-app-per-user (cleanest per I8).

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
| `POSITIONING.md` | Sharpened messaging spine — one-liner, who-it's-for, pillars, competitive framing, landing-hero draft. The "say this, not that" reference for all afair-facing copy (English, no first-person, humanizer before publish). Sharpened post-GBrain | When the wedge shifts or a competitor forces a reframe |
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
| `analysis/2026-06-13-gbrain-competitive-analysis.md` | Deep-dive on GBrain (garrytan/gbrain) — strongest adjacent entrant. Honest overlap (it leads on memory-engine quality), afair's defensible wedge (product-form + EU + emergent ontology + durability), strategic do/don't. Summarized in VISION §9.1 | Point-in-time snapshot; refresh if GBrain productizes or at next competitive review |
| `analysis/2026-06-13-feature-response-to-gbrain.md` | Curated BUILD/DEFER/WON'T against GBrain's capabilities. #1 = recall honesty layer ("what your memory doesn't know yet", I1-additive); explicit non-goals (auto-ingestion daemon, multi-tenant, imposed ontology, feature-race) | Update as the build queue ships |
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
