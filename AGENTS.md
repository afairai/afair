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

## Design (binding) — impeccable mandatory

Any UI / frontend work goes through **impeccable**. No freehand UI generation. This is a binding global rule across every AI harness (Claude Code, Codex, Cursor, Copilot, Aider).

- Invoke a precise sub-command for every UI task: `/impeccable craft|shape|audit|critique|polish|colorize|typeset|animate|bolder|quieter|layout|clarify|distill|harden|delight|overdrive|adapt|onboard|optimize|extract|document|live|init <target>`.
- Pre-merge gate: `npx impeccable detect src/` (41 deterministic anti-pattern rules, CI-friendly).
- Bootstrap once per project: `yes | npx -y impeccable skills install`, then `/impeccable init`.
- Exceptions: non-UI work, one-line typo/aria fixes, or explicit user "skip impeccable for this".
- Full rules: `~/.claude/rules/design.md`.
