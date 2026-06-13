# Claude.ai (web + desktop): connecting to afair

Claude.ai connects to custom MCP servers through the **Connectors** UI, not
a config file. afair speaks the full OAuth 2.1 flow (Dynamic Client
Registration + PKCE), so the web path needs only the server URL: the
browser handles sign-in and no token is pasted by hand.

## 1. Add the connector

In Claude.ai (web or desktop):

1. **Settings** then **Connectors** then **Add custom connector**
2. **Name:** `afair`
3. **Server URL:** `https://mcp.afair.ai/mcp`
4. **Save.** Claude.ai discovers the OAuth metadata automatically and opens a
   browser approval step. Sign in there (GitHub identity through
   afair.ai), approve the `mcp` scope, and the connector links itself. No
   bearer token to copy.

That is the same OAuth flow ChatGPT and Perplexity use, so a connector that
works in one web client works in all of them.

> **Local-only alternative.** If you prefer not to round-trip through the
> hosted OAuth step, the CLI clients (Claude Code, Codex, Cursor) connect
> with the static bearer token instead and originate the request from your
> own machine. See [claude-code.md](claude-code.md).

## 2. Add the instruction snippet

In Claude.ai:

1. **Settings** then **Profile** then **Custom Instructions** (account-wide), OR
2. **Project** then **Instructions** (per-project, recommended)

Paste the contents of [_snippet.md](_snippet.md) into the instructions
field. The connector establishes the surface; the snippet establishes the
habit of actually reaching for the tools.

## 3. Verify

In a new conversation, with the connector enabled (toggle it on in the
conversation's tools menu), ask:

> Use the afair connector to list the available tools.

Expected: three tools, `remember`, `recall`, `observe`. Then a round-trip:

> Use afair to remember: "Claude.ai web verification 2026-06-13"

Open a new conversation:

> Recall what we saved about 2026-06-13 from Claude.ai.

The fact should be findable. Better still, save from Claude Code and recall
from Claude.ai, or the reverse. That cross-vendor moment is the point of
the project.

## Troubleshooting

### The browser approval never returns

The OAuth approval opens, you sign in, but Claude.ai does not register the
connector. Confirm the discovery endpoints are reachable from your network:

```bash
curl -s https://mcp.afair.ai/.well-known/oauth-authorization-server | head -c 200
```

Expected: a JSON document listing `authorization_endpoint`, `token_endpoint`,
`registration_endpoint`, and `code_challenge_methods_supported: ["S256"]`.
If that returns 200, the server side is healthy; retry the connector or
restart Claude.ai.

### "Server returned no tools"

The connector linked but `tools/list` came back empty. Check the server
directly with a bearer token:

```bash
TOKEN=$(grep '^AFAIR_AUTH_TOKEN=' .env.local | cut -d= -f2-)
curl -X POST https://mcp.afair.ai/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

Three tools means the server is fine and the client needs a reconnect.

### Data residency

Claude.ai's MCP calls originate from Anthropic's servers (likely US), which
then reach the Fly machine in `fra`. The substrate stays in the EU (your Fly
volume); the only transient cross-region hop is the MCP request itself. For
strict EU-only flow, prefer Claude Code or Claude Desktop running locally,
which originate the call from your own machine.
