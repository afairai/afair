<p align="center">
  <img src="assets/logo/afair-elephant.png" alt="afair" width="160" />
</p>

<h1 align="center">afair</h1>

<p align="center">
  <em>A user-owned, vendor-neutral, self-organizing cognitive memory layer for AI agents — built behind a stable MCP surface, free to mutate beneath it.</em>
</p>

---

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

Licensed under the GNU Affero General Public License v3.0. See `LICENSE` for the full text.

AGPLv3 is the deliberate choice. afair is built so users can take their memory anywhere; the same posture applies to the code. Anyone can self-host, fork, modify. Network-service forks must publish their modifications under the same license. The hosted offering at afair.ai is one possible deployment, not the only one.

In one line: free to use, free to host yourself, must share back if you run it as a service for others.
