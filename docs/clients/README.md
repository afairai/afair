# MCP client integration

Connect any MCP-speaking AI client to your afair vault.

**`<your-vault-url>`** is wherever your vault runs:
- **Local self-host:** `http://127.0.0.1:8765` (the default after `uv run python -m afair`).
- **Deployed self-host:** your own domain or Fly app, e.g. `https://your-app.fly.dev`.
- **Hosted afair.ai** (coming soon): the per-user address shown in your dashboard.

**Server endpoint:** `<your-vault-url>/mcp`
**Auth:** a static bearer token or the OAuth browser sign-in (see
[Two ways to authenticate](#two-ways-to-authenticate) below). A local self-host
runs without auth, so there is no token and no header.
**Health:** `<your-vault-url>/health` (no auth required)

## The easy path: one command

```bash
uv run python scripts/install_clients.py             # interactive picker
uv run python scripts/install_clients.py --yes        # all detected, no prompt
uv run python scripts/install_clients.py --only copilot   # just one
uv run python scripts/install_clients.py --dry-run    # preview, change nothing
```

This script:

- On a terminal, shows an **interactive picker** so it never installs everywhere
  by surprise. Pick by number or name. A piped / CI run (no TTY) installs into
  all detected clients, and `--yes` does the same without prompting.
- `--only <a,b>` / `--skip <a,b>` choose non-interactively; `--list` prints the
  client keys (`claude-code`, `codex`, `cursor`, `copilot`, `claude-ai`).
- Reads the token from `.env.local`
- Detects Claude Code, Codex CLI, Cursor, and GitHub Copilot (VS Code); writes
  each one's MCP server config and appends the instruction snippet to its
  CLAUDE.md / AGENTS.md / rules
- **Web clients (Claude.ai) are only offered against a public URL.** They run in
  the vendor's cloud and can't reach your `localhost`, so a local self-host can't
  serve them, the picker says so and skips them.
- Backs up every file it touches (`<path>.bak.<timestamp>`); idempotent, running
  twice is safe

After running, **restart any open MCP clients** (Claude Code, Cursor, etc.)
to pick up the new server. Claude.ai is UI-only; see [claude-ai.md](claude-ai.md).

The per-client docs below explain what the script does, what to tweak for
non-default setups, and how to troubleshoot.

## Two ways to authenticate

The server speaks both. Which you use depends on the client and the deployment,
not on a fixed rule:

- **Bearer token:** a static `AFAIR_AUTH_TOKEN` in an `Authorization` header.
  Any client that lets you set headers can use it. Simplest for a local
  self-host, and the only option against a loopback address with no public URL
  to run OAuth against. (A local instance runs without auth, so there is no
  token at all.)
- **OAuth 2.1:** DCR + PKCE, browser sign-in, nothing to paste. Required by the
  web clients (they cannot set a header or reach localhost), and also supported
  by the CLI/desktop clients when they connect to a public vault.

For a **public deployment**, prefer OAuth everywhere: a real per-user login
beats a shared static token sitting in config files. Keep the bearer for a
**local** self-host or a headless setup.

## Per-client setup

| Client | Auth | Setup guide |
|---|---|---|
| [Claude Code (CLI)](claude-code.md) | bearer or OAuth | HTTP transport |
| [Codex CLI](codex.md) | bearer or OAuth | `~/.codex/config.toml` |
| [Cursor](cursor.md) | bearer or OAuth | `.cursor/mcp.json` |
| [GitHub Copilot (VS Code)](copilot.md) | bearer or OAuth | VS Code user `mcp.json` (agent mode) |
| [Claude.ai (web/desktop)](claude-ai.md) | OAuth | Custom Connector UI |
| [ChatGPT (web)](chatgpt.md) | OAuth (plan-gated) | Connectors / Developer mode |
| [Perplexity (web)](perplexity.md) | OAuth (plan-gated) | Connectors |

The web clients share one OAuth flow, so a connector that works in one works in
all. "plan-gated" means custom MCP connectors are a paid-tier surface in that
product, not an afair limitation.

## The two pieces every client needs

1. **Connection config**: how the client *finds* the server (URL + auth header).
2. **Instruction snippet**: pasted into the client's CLAUDE.md / AGENTS.md / `.cursorrules` / etc. so the AI *reaches for the tools* in daily work. See [_snippet.md](_snippet.md).

The first one establishes the surface; the second one establishes the habit. Both are needed.

## Where to get the token

```bash
# From your local checkout, never echo it to a terminal you don't trust
grep '^AFAIR_AUTH_TOKEN=' .env.local | cut -d= -f2-
```

Copy that value where the client's config says `<AFAIR_AUTH_TOKEN>`. If you deployed to Fly, the token is also in your app's secrets (`fly secrets list -a <your-app>`, digest only). A local self-host instance has no token, so skip this and leave the `Authorization` header out.

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
curl -s <your-vault-url>/health
# {"status":"ok"}

# OAuth 2.1 authorization-server metadata (RFC 8414)
curl -s <your-vault-url>/.well-known/oauth-authorization-server
# issuer, authorization_endpoint, token_endpoint,
# registration_endpoint (Dynamic Client Registration),
# revocation_endpoint, code_challenge_methods_supported: ["S256"] (PKCE),
# grant_types_supported: ["authorization_code","refresh_token"]

# Protected-resource metadata (RFC 9728)
curl -s <your-vault-url>/.well-known/oauth-protected-resource
# resource: <your-vault-url>/mcp, authorization_servers, scopes_supported

# Unauthenticated MCP request returns the discovery challenge
curl -s -D - -o /dev/null <your-vault-url>/mcp -H "Accept: text/event-stream"
# HTTP/2 401
# www-authenticate: Bearer realm="afair",
#   resource_metadata="<your-vault-url>/.well-known/oauth-protected-resource"
```

That `401` plus `WWW-Authenticate` is what makes a web client (Claude.ai,
ChatGPT, Perplexity) discover the OAuth flow on its own and start the
browser approval. The CLI clients skip it and send the bearer token
directly. Both paths reach the same three tools.

**Still needs a human:** the final per-client click-through (approve in the
browser, confirm `remember`/`recall`/`observe` show up, do a save-here /
recall-there round-trip) is the Phase 0 capability gate. The server side above
is verified; the per-vendor UI walk is what each daily-use session confirms.
