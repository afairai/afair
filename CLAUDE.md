# Project memory — neverforget

> Read `VISION.md` first. It is the operating constitution. The invariants in §4 are inviolable.
> This file is for project-specific working rules that complement (never override) the constitution.

## 0. Current state

**Phase:** 0 — Substrate + MCP Surface (cross-vendor, zero automated ingestion)
**Status:** In execution
**Audience:** solo build; future contributors

### 0.1 What's live

- VISION.md (the constitution)
- Repo scaffold: pyproject.toml, Dockerfile, fly.toml, .env templates, CLAUDE.md

### 0.2 What's in flight

- Task #1 — repo scaffold (this work)
- Tasks #2–#7 — substrate, MCP server, extractor, Fly deploy, cross-vendor verification, capability-gate journal

### 0.3 What's blocked

- Nothing yet

## 1. Codename and renaming discipline

`neverforget` is the **codename**. Final product name is deferred to Phase 6–7 (see VISION.md §15).

**Rule for code:** keep external surface configurable so the rename touches metadata, not prose.

- Internal Python imports are **relative** (`from . import substrate`), never `from neverforget.substrate import ...`.
- The package directory `neverforget/` is fine — it's one mechanical rename when the time comes.
- Avoid hardcoding the codename in docstrings, log messages, error strings, README body text. Write generically.
- The MCP server name surface (visible to clients), Fly app name, Docker image, repo name — keep the codename for now, accept that renaming these means a one-shot find-replace + redeploy.

## 2. Stack (decided in VISION.md §6.8)

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

## 3. Trust ladder (binding for all phases)

Per VISION.md §9 Phase 0:

1. **Phase 0** — explicit `remember` / `observe` calls only. Zero automated ingestion.
2. **Phase 1+** — user-initiated manual import (paste, drag, "ingest this URL").
3. **Phase 2+** — opt-in connectors, one source at a time, salience filter auditable before enable.
4. **Continuous sensing** (Gmail, Calendar, Slack, Drive) — earned, not assumed at install.

If a feature proposal requires accessing user data the user hasn't deliberately handed in, the answer is no until the trust ladder reaches that rung.

## 4. Documentation registry

| File | Purpose | Update cadence |
|---|---|---|
| `VISION.md` | The constitution — invariants, architecture, phase plan | Quarterly review; §0/§9 updates each phase |
| `CLAUDE.md` (this file) | Project-specific working rules + current state | After each merge that changes state |
| `README.md` | Public-facing setup + orientation | When setup steps change |
| `.env.example` | Required env vars with comments | When env shape changes |
| `.env.secrets.backup` | Canonical secrets backup (gitignored) | Whenever a secret is created/rotated |
| `docs/clients/*.md` | Per-client MCP connection config | When client integration changes |
| `analysis/phase-0-journal.md` | Daily-use log for the Phase 0 capability gate | Daily during the two-week window |

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

- `neverforget/substrate/` — see `.claude/rules/substrate.md` (forthcoming, task #2)
- `neverforget/mcp/` — see `.claude/rules/mcp.md` (forthcoming, task #3)
- `neverforget/agents/` — see `.claude/rules/agents.md` (forthcoming, task #4)

## 7. Quick commands

```bash
# install deps
uv sync

# run server locally
uv run python -m neverforget

# run tests
uv run pytest

# type check
uv run mypy neverforget

# lint
uv run ruff check
uv run ruff format

# build docker image
docker build -t neverforget .

# deploy to Fly (after fly.toml is configured)
fly deploy
```
