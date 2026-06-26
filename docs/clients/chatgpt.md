# ChatGPT: connecting to afair

ChatGPT reaches remote MCP servers through **connectors** (Settings then
Connectors, exposed under Developer mode / custom connectors depending on
your plan). afair speaks OAuth 2.1 with Dynamic Client Registration and
PKCE, so ChatGPT connects from the server URL alone: the browser handles
sign-in, nothing is pasted by hand.

> **Availability.** Custom MCP connectors in ChatGPT roll out by plan and
> region (Plus / Pro / Business / Enterprise, with admin enablement on the
> business tiers). If you do not see a "custom connector" or "Developer
> mode" option, the surface is not enabled for your account yet. The CLI
> clients ([claude-code.md](claude-code.md), [codex.md](codex.md)) give you
> the full capability gate in the meantime.

## 1. Add the connector

1. **Settings** then **Connectors** (enable **Developer mode** if your plan
   gates custom connectors behind it).
2. **Add / Create** a custom connector.
3. **Name:** `afair`
4. **MCP Server URL:** `<your-vault-url>/mcp`
5. **Authentication:** OAuth (the default for a server that advertises it).
   Save, then complete the browser approval: sign in (GitHub identity
   through afair.ai), approve the `mcp` scope. The connector links itself.

## 2. Add the instruction snippet

ChatGPT does not have a per-connector instruction field, so put the snippet
where ChatGPT reads standing instructions:

- **Custom Instructions:** Settings then Personalization then Custom
  Instructions, or
- **A Project's instructions** (recommended) if you keep afair work in a
  Project.

Paste the contents of [_snippet.md](_snippet.md).

## 3. Verify

In a new chat with the connector enabled, ask:

> Use the afair connector to list its tools.

Expected: `remember`, `recall`, `observe`. Then a cross-vendor round-trip,
save here and recall from another client:

> Use afair to remember: "ChatGPT verification 2026-06-13"

Then, in Claude Code or Claude.ai:

> Recall what afair has about 2026-06-13.

## Troubleshooting

### No custom-connector option

Custom MCP connectors are plan and region gated. Check that Developer mode
is on; on business tiers a workspace admin may need to allow custom
connectors first.

### Approval loops or fails

Confirm the discovery document is reachable:

```bash
curl -s <your-vault-url>/.well-known/oauth-protected-resource
```

Expected: JSON naming `<your-vault-url>/mcp` as the resource and
`<your-vault-url>` as the authorization server. A 200 here means the
server is healthy and the retry is on the client side.

### Data residency

ChatGPT's MCP calls originate from OpenAI's servers and then reach the Fly
machine in `fra`. The substrate stays in the EU; only the transient request
hop crosses regions. For strict EU-only flow, use a locally running CLI
client.
