# MCP client integration

Connect any MCP-speaking AI client to the deployed afair server.

**Server endpoint:** `https://afair.fly.dev/mcp`
**Auth:** `Authorization: Bearer <AFAIR_AUTH_TOKEN>` on every request
**Health:** `https://afair.fly.dev/health` (no auth required)

## The easy path — one command

```bash
uv run python scripts/install_clients.py --dry-run   # preview
uv run python scripts/install_clients.py             # apply
```

This script:

- Reads the token from `.env.local`
- Detects Claude Code, Codex CLI, and Cursor on your machine
- Writes the MCP server config to each one's settings file
- Appends the instruction snippet to each one's CLAUDE.md / AGENTS.md / rules
- Backs up every file it touches (`<path>.bak.<timestamp>`)
- Is idempotent — running twice is safe

After running, **restart any open MCP clients** (Claude Code, Cursor, etc.)
to pick up the new server. Claude.ai is UI-only; see [claude-ai.md](claude-ai.md).

The per-client docs below explain what the script does, what to tweak for
non-default setups, and how to troubleshoot.

## Per-client setup

| Client | Status | Setup guide |
|---|---|---|
| [Claude Code (CLI)](claude-code.md) | ✅ Recommended | HTTP transport + `headers` |
| [Codex CLI](codex.md) | ✅ Supported | HTTP transport in `~/.codex/config.toml` |
| [Claude.ai (web/desktop)](claude-ai.md) | ⚠️ See note | Custom Connector UI |
| [Cursor](cursor.md) | ✅ Supported | `.cursor/mcp.json` |

## The two pieces every client needs

1. **Connection config** — how the client *finds* the server (URL + auth header).
2. **Instruction snippet** — pasted into the client's CLAUDE.md / AGENTS.md / `.cursorrules` / etc. so the AI *reaches for the tools* in daily work. See [_snippet.md](_snippet.md).

The first one establishes the surface; the second one establishes the habit. Both are needed.

## Where to get the token

```bash
# From your local checkout — never echo it to a terminal you don't trust
grep '^AFAIR_AUTH_TOKEN=' .env.local | cut -d= -f2-
```

Copy that value where the client's config says `<AFAIR_AUTH_TOKEN>`. If you've never opened `.env.local`, the token is also visible in the Fly dashboard at `https://fly.io/apps/afair/secrets` (digest only, not the value).

## Verification

After setting up a client, in that client ask:

> "Use the afair MCP server to list the tools available."

You should see three tools: `remember`, `recall`, `observe`. Then:

> "Use afair to remember: 'first cross-vendor verification on 2026-05-25'"

Then (in the same client OR a different one):

> "What did we remember about 2026-05-25?"

The AI should call `recall` and return the fact. That round-trip is the Phase 0 capability gate.

For a non-interactive end-to-end smoke that doesn't require any AI client at all, run `scripts/smoke.sh` from the project root.
