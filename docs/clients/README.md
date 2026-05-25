# MCP client integration

Connect any MCP-speaking AI client to the deployed neverforget server.

**Server endpoint:** `https://neverforget.fly.dev/mcp`
**Auth:** `Authorization: Bearer <NEVERFORGET_AUTH_TOKEN>` on every request
**Health:** `https://neverforget.fly.dev/health` (no auth required)

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
grep '^NEVERFORGET_AUTH_TOKEN=' .env.local | cut -d= -f2-
```

Copy that value where the client's config says `<NEVERFORGET_AUTH_TOKEN>`. If you've never opened `.env.local`, the token is also visible in the Fly dashboard at `https://fly.io/apps/neverforget/secrets` (digest only, not the value).

## Verification

After setting up a client, in that client ask:

> "Use the neverforget MCP server to list the tools available."

You should see four tools: `remember`, `recall`, `list_context`, `observe`. Then:

> "Use neverforget to remember: 'first cross-vendor verification on 2026-05-25'"

Then (in the same client OR a different one):

> "What did we remember about 2026-05-25?"

The AI should call `recall` and return the fact. That round-trip is the Phase 0 capability gate.

For a non-interactive end-to-end smoke that doesn't require any AI client at all, run `scripts/smoke.sh` from the project root.
