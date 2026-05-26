# afair

> A user-owned, vendor-neutral, self-organizing cognitive memory layer for AI agents — built behind a stable MCP surface, free to mutate beneath it.

**Codename. Final name deferred to Phase 6–7.** See `VISION.md §15`.

## What this is

A Model Context Protocol (MCP) server that gives any MCP-speaking AI client — Claude Code, Codex CLI, Cursor, Claude.ai, Copilot, Windsurf — a persistent, append-only memory substrate that the **user owns**, not the AI vendor.

The constitutional invariants are documented in `VISION.md §4`. Read those first.

## Status

**Phase 0 — Substrate + MCP Surface (cross-vendor, zero automated ingestion).**

The three MCP tools — `remember`, `recall`, `observe` — are the forever contract per Invariant I1. Nothing reaches into your life uninvited; data enters the substrate only via explicit calls. (`recall` is the single retrieval verb; survey, single-fetch, and full-payload modes are kwargs, not separate tools.)

## Local dev

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env
# fill in ANTHROPIC_API_KEY (or your chosen provider key)
uv run python -m afair
```

## Architecture

See `VISION.md §6`. Briefly:

- **Substrate** (append-only SQLite + FTS5 + sqlite-vec) — immutable, content-addressed event log
- **Interpretation layer** — versioned materialized views over the substrate, regenerable
- **MCP Surface** — stable, versioned, additive contract for external AI clients
- **Agents** — context-aware Extractor in Phase 0; full swarm grows in later phases

## License

AGPL-3.0-or-later. License decision is provisional per `VISION.md §13 Open Questions` and may change before public release.
