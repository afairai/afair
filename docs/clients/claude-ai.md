# Claude.ai (web + desktop) — connecting to afair

Claude.ai connects to custom MCP servers via the **Connectors** UI, not a
config file. The exact path varies by surface but the steps are the same.

> ⚠️ **Known issue, status uncertain (2026-05).** Several reports describe
> Claude.ai failing to authenticate with custom MCP servers that use
> non-OAuth bearer tokens — see [anthropics/claude-ai-mcp issue #164](https://github.com/anthropics/claude-ai-mcp/issues/164).
> The issue is marked closed but the resolution isn't visible to outsiders.
>
> **If your Claude.ai connection fails with auth errors:** Claude Code CLI
> and Codex CLI both work via bearer token today and let you exercise the
> full Phase 0 capability gate while this is sorted upstream. We'll layer
> proper OAuth (Clerk or equivalent) in a later phase, which the Claude.ai
> bug appears to be specifically about.

## 1. Add the connector

In Claude.ai (web or desktop):

1. **Settings** → **Connectors** → **Add custom connector**
2. **Name:** `afair`
3. **Server URL:** `https://afair.fly.dev/mcp`
4. **Authentication:** "Bearer Token" / "API Key" / "Custom Header" (the
   label varies between Claude.ai versions). Use:
   - Header name: `Authorization`
   - Header value: `Bearer <AFAIR_AUTH_TOKEN>`
5. **Save**

If your Claude.ai version doesn't expose a custom-header field and only
allows OAuth, this is the bug above — bearer-token clients can't connect
yet. Use Claude Code or Codex meanwhile.

## 2. Add the instruction snippet

In Claude.ai:

1. **Settings** → **Profile** → **Custom Instructions** (account-wide), OR
2. **Project** → **Instructions** (per-project, recommended)

Paste the contents of [_snippet.md](_snippet.md) into the instructions
field.

## 3. Verify

In a new conversation, with the connector enabled (toggle it on in the
conversation's tools/connectors menu), ask:

> Use the afair connector to list the available tools.

Expected: three tools — `remember`, `recall`, `observe`.

Round-trip:

> Use afair to remember: "Claude.ai web verification 2026-05-25"

Open a new conversation:

> Recall what we saved about 2026-05-25 from Claude.ai.

The fact should be findable. Even better — save from Claude Code, then
recall from Claude.ai, or vice versa. That cross-vendor moment is what
makes the project valuable.

## Troubleshooting

### "Authorization with the MCP server failed"

This is the known bug pattern. As of 2026-05, the resolution is not yet
visible in the open. Options:

1. **Wait for an upstream fix** (Anthropic is tracking the issue).
2. **Use Claude Code or Codex** for now — both work with bearer tokens.
3. **Upgrade to OAuth-based auth** when we ship that layer.

### "Server returned no tools"

The connector connected but `tools/list` returned empty. Verify the
server has tools registered:

```bash
TOKEN=$(grep '^AFAIR_AUTH_TOKEN=' .env.local | cut -d= -f2-)
# Initialize MCP session, then list tools
curl -X POST https://afair.fly.dev/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

If that returns four tools, the server is fine; Claude.ai's MCP client
has an issue. Restart Claude.ai or reconnect the connector.

### Data residency

Claude.ai's MCP calls originate from Anthropic's servers (probably US),
which then talk to the Fly machine in `fra`. The substrate stays in EU
(your Fly volume); the only transient cross-region hop is the MCP request
itself. For strict EU-only data flow, prefer Claude Code or Claude
Desktop running locally on your laptop — those originate the MCP call
from your own machine.
