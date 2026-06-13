# MCP client integration

Connect any MCP-speaking AI client to the deployed afair server.

**Server endpoint:** `https://mcp.afair.ai/mcp`
**Auth:** `Authorization: Bearer <AFAIR_AUTH_TOKEN>` on every request
**Health:** `https://afair.fly.dev/health` (no auth required)

## The easy path: one command

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
- Is idempotent, running twice is safe

After running, **restart any open MCP clients** (Claude Code, Cursor, etc.)
to pick up the new server. Claude.ai is UI-only; see [claude-ai.md](claude-ai.md).

The per-client docs below explain what the script does, what to tweak for
non-default setups, and how to troubleshoot.

## Per-client setup

**CLI clients** use the static bearer token and originate the request from
your own machine (best for strict EU-only data flow):

| Client | Status | Setup guide |
|---|---|---|
| [Claude Code (CLI)](claude-code.md) | ✅ Recommended | HTTP transport + `headers` |
| [Codex CLI](codex.md) | ✅ Supported | HTTP transport in `~/.codex/config.toml` |
| [Cursor](cursor.md) | ✅ Supported | `.cursor/mcp.json` |

**Web clients** connect by server URL alone; the hosted OAuth flow (DCR +
PKCE, browser sign-in) handles auth, so there is no token to paste:

| Client | Status | Setup guide |
|---|---|---|
| [Claude.ai (web/desktop)](claude-ai.md) | ✅ OAuth | Custom Connector UI |
| [ChatGPT (web)](chatgpt.md) | ✅ OAuth (plan-gated) | Connectors / Developer mode |
| [Perplexity (web)](perplexity.md) | ✅ OAuth (plan-gated) | Connectors |

The web clients share one OAuth flow, so a connector that works in one works
in all. "plan-gated" means custom MCP connectors are a paid-tier surface in
that product, not an afair limitation.

## The two pieces every client needs

1. **Connection config**: how the client *finds* the server (URL + auth header).
2. **Instruction snippet**: pasted into the client's CLAUDE.md / AGENTS.md / `.cursorrules` / etc. so the AI *reaches for the tools* in daily work. See [_snippet.md](_snippet.md).

The first one establishes the surface; the second one establishes the habit. Both are needed.

## Where to get the token

```bash
# From your local checkout, never echo it to a terminal you don't trust
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

## Server capabilities (verified live 2026-06-13)

Any MCP client that follows the spec connects, because the server advertises
the standard discovery surface. These checks are reproducible and need no
auth, run them to confirm the server is reachable before debugging a client:

```bash
# Health
curl -s https://mcp.afair.ai/health
# {"status":"ok"}

# OAuth 2.1 authorization-server metadata (RFC 8414)
curl -s https://mcp.afair.ai/.well-known/oauth-authorization-server
# issuer, authorization_endpoint, token_endpoint,
# registration_endpoint (Dynamic Client Registration),
# revocation_endpoint, code_challenge_methods_supported: ["S256"] (PKCE),
# grant_types_supported: ["authorization_code","refresh_token"]

# Protected-resource metadata (RFC 9728)
curl -s https://mcp.afair.ai/.well-known/oauth-protected-resource
# resource: https://mcp.afair.ai/mcp, authorization_servers, scopes_supported

# Unauthenticated MCP request returns the discovery challenge
curl -s -D - -o /dev/null https://mcp.afair.ai/mcp -H "Accept: text/event-stream"
# HTTP/2 401
# www-authenticate: Bearer realm="afair",
#   resource_metadata="https://mcp.afair.ai/.well-known/oauth-protected-resource"
```

That `401` plus `WWW-Authenticate` is what makes a web client (Claude.ai,
ChatGPT, Perplexity) discover the OAuth flow on its own and start the
browser approval. The CLI clients skip it and send the bearer token
directly. Both paths reach the same three tools.

**Still needs a human:** the final per-client click-through (approve in the
browser, confirm `remember`/`recall`/`observe` show up, do a save-here /
recall-there round-trip) is the Phase 0 capability gate, tracked in the
[journal](../../analysis/phase-0-journal.md). The server side above is
verified; the per-vendor UI walk is what each daily-use session confirms.
