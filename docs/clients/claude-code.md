# Claude Code (CLI) — connecting to afair

Claude Code's `.mcp.json` lives at three possible scopes; pick the one that
matches your workflow:

| Scope | File | Who benefits |
|---|---|---|
| User-global | `~/.claude/settings.json` (under `mcpServers`) | All your projects |
| Project | `<project>/.mcp.json` | Anyone working on this repo |
| Local | `<project>/.claude/settings.local.json` | You only, on this machine |

For a personal vault, user-global is usually right.

## 1. Add the server

Add to `~/.claude/settings.json`:

```jsonc
{
  "mcpServers": {
    "afair": {
      "type": "http",
      "url": "https://mcp.afair.ai/mcp",
      "headers": {
        "Authorization": "Bearer <AFAIR_AUTH_TOKEN>"
      }
    }
  }
}
```

Replace `<AFAIR_AUTH_TOKEN>` with the value from your `.env.local`
(or from your password manager — never paste it in chat or commit it).

You can also do it via the CLI:

```bash
claude mcp add afair \
  --transport http \
  --url https://mcp.afair.ai/mcp \
  --header "Authorization=Bearer <AFAIR_AUTH_TOKEN>"
```

## 2. Add the instruction snippet

Append the contents of [_snippet.md](_snippet.md) to `~/.claude/CLAUDE.md`
(or to your project's `CLAUDE.md` if you want it scoped).

## 3. Verify

Restart Claude Code (or reload the MCP server with `/mcp` then reconnect).
Then ask:

> Use the afair MCP server to list the tools available.

Expected: three tools listed — `remember`, `recall`, `observe`.

Now exercise the round-trip:

> Use afair to remember: "first claude-code verification on
> 2026-05-25, the round-trip works"

Then in **the same conversation** (proving the tool works at all):

> What did we just remember about 2026-05-25?

Then in **a brand-new conversation** (proving persistence across sessions):

> Recall everything you know about 2026-05-25.

Both should surface the fact you saved. That's the capability gate.

## Troubleshooting

### "Tool not found" / nothing happens

The MCP server didn't connect. Run `/mcp` in Claude Code to see connection
status. Common causes:

- Wrong URL (must end with `/mcp/` with trailing slash)
- Missing `Authorization` header
- Wrong token

### 401 errors in `/mcp` status

Token is wrong. Verify locally:

```bash
TOKEN=$(grep '^AFAIR_AUTH_TOKEN=' .env.local | cut -d= -f2-)
curl -s -o /dev/null -w "%{http_code}\n" \
  -X POST https://mcp.afair.ai/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}'
```

`307` (redirect) or anything 2xx in that test = token good, Claude Code
config is wrong. `401` = token wrong, regenerate.

### Server returns 503

The Fly machine is degraded. Check:

```bash
fly logs --app afair
```

Usually a substrate-DB issue at boot. Most often resolved by
`fly machine restart 1859472c239438 --app afair`.

### Auto-accept-edits + MCP tools

Claude Code's `Shift+Tab` auto-accept-edits mode applies to file
edits only; MCP tool calls still go through the permission prompt the
first time. After approving once, subsequent calls don't re-prompt within
the session.
