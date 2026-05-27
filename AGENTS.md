# AGENTS.md

> This file exists for AI assistants that look for `AGENTS.md` at the
> project root by convention (Codex CLI, Cursor, etc.). It is **not**
> the canonical project memory — that lives in `CLAUDE.md`.

## Read order for any AI assistant working in this repo

1. **`VISION.md`** — the operating constitution. Invariants in §4 are inviolable.
2. **`CLAUDE.md`** — project-specific working rules, current state, stack, naming, trust ladder, documentation registry. Read in full.
3. **`README.md`** — public-facing orientation; setup; status.
4. Path-scoped detail as needed (`docs/operations.md`, `docs/clients/`, `analysis/`).

## TL;DR for assistants

- `afair` is the final product name (registered at `afair.ai`).
- The MCP surface (`remember`, `recall`, `observe`) is frozen per Invariant I1. Adding or breaking that surface is forbidden.
- Substrate is append-only per Invariant I2. No mutation, no deletion paths outside the documented right-to-erasure flow in `docs/operations.md §7`.
- Tests + types must stay green: `uv run pytest`, `uv run mypy afair`, `uv run ruff check`. The deploy gate runs all three.

Everything else — code conventions, deploy recipes, asset pipeline, current phase status, captured-for-later designs — is in `CLAUDE.md` and the documents it links.
