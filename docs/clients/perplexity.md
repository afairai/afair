# Perplexity: connecting to afair

Perplexity connects to remote MCP servers from its **Connectors** settings
(web and desktop; availability depends on plan, custom MCP connectors are a
Pro / Enterprise surface). afair speaks OAuth 2.1 with Dynamic Client
Registration and PKCE, so the connection needs only the server URL and a
browser approval step.

> **Availability.** Custom MCP connectors in Perplexity roll out by plan.
> If your account exposes only the built-in connectors and no "add custom
> MCP server" field, the surface is not enabled yet. Use a CLI client
> ([claude-code.md](claude-code.md), [codex.md](codex.md)) for the full
> capability gate meanwhile.

## 1. Add the connector

1. **Settings** then **Connectors** then **Add connector** / **Add custom
   MCP server**.
2. **Name:** `afair`
3. **Server URL:** `https://mcp.afair.ai/mcp`
4. **Authentication:** OAuth. Save and complete the browser approval: sign
   in (GitHub identity through afair.ai), approve the `mcp` scope. The
   connector links itself, no token to paste.

## 2. Add the instruction snippet

Put the snippet where Perplexity reads standing instructions for your
account or Space (per-Space instructions are the cleaner home if you keep
afair work in one Space). Paste the contents of [_snippet.md](_snippet.md).

## 3. Verify

In a new thread with the connector enabled:

> Use the afair connector to list its tools.

Expected: `remember`, `recall`, `observe`. Then save here and recall
elsewhere:

> Use afair to remember: "Perplexity verification 2026-06-13"

Then from Claude Code or ChatGPT:

> Recall what afair has about 2026-06-13.

## Troubleshooting

### No custom-MCP option

Custom connectors are plan gated in Perplexity. Confirm your plan exposes
custom MCP servers; built-in connectors do not count.

### Approval fails

Confirm the OAuth discovery is reachable:

```bash
curl -s https://mcp.afair.ai/.well-known/oauth-authorization-server | head -c 200
```

A 200 with `registration_endpoint` and `code_challenge_methods_supported:
["S256"]` means the server side is healthy.

### Data residency

Perplexity's MCP calls originate from its servers, then reach the Fly
machine in `fra`. The substrate stays in the EU; only the request hop
crosses regions. For strict EU-only flow, use a locally running CLI client.
