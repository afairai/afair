# Cursor — connecting to afair

Cursor supports MCP servers via `.cursor/mcp.json` at user or project scope.

## 1. Add the server

Create `~/.cursor/mcp.json` (or `<project>/.cursor/mcp.json`):

```jsonc
{
  "mcpServers": {
    "afair": {
      "type": "http",
      "url": "<your-vault-url>/mcp",
      "headers": {
        "Authorization": "Bearer <AFAIR_AUTH_TOKEN>"
      }
    }
  }
}
```

Or via the Cursor UI:

1. **Settings** (⌘ ,) → **MCP** → **Add Server**
2. Paste the JSON, or fill the form
3. **Enable**

## 2. Add the instruction snippet

Cursor reads instructions from a few places — pick one or both:

- **Project-scoped:** create `.cursor/rules/afair.md` and paste the
  contents of [_snippet.md](_snippet.md).
- **Workspace-scoped:** add to `.cursorrules` at the repo root.
- **Global (Cursor-wide):** Settings → Rules → "Rules for AI", paste the
  snippet.

## 3. Verify

Restart Cursor. In a chat with the AI:

> Use the afair MCP server to list its tools.

Expected: three tools — `remember`, `recall`, `observe`.

Round-trip:

> Use afair to remember: "Cursor verification 2026-05-25, the
> cross-vendor stack is working"

Then in a new Cursor chat (or from another client):

> What did we save in afair about 2026-05-25?

## Troubleshooting

### Cursor's MCP indicator shows "disconnected" or red dot

Most often a config typo. Cursor logs MCP errors to the Output panel under
"MCP" — check there. Common issues:

- URL missing trailing slash on `/mcp/`
- Bearer token has extra whitespace or a newline (Cursor doesn't tolerate
  trailing `\n` in header values; check your paste)
- JSON syntax error in `mcp.json` (run it through `jq` to validate)

### Tool calls feel slow

Cursor sometimes adds 1–2 s of latency around MCP calls. For the
`recall` shallow path (FTS5 only), the actual server response is well
under 100 ms — anything more is Cursor's MCP client overhead. We can't
fix that from the server side.

### Cursor and parallel agents

If you run multiple Cursor windows simultaneously, each MCP client
instance connects with the same token. Single-tenant means they all share
the same vault, which is what we want.
