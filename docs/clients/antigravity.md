# Antigravity: connecting to afair

Antigravity (Google's agentic editor) reads MCP servers from a JSON config
shared by its IDE and CLI.

> **Field gotcha:** Antigravity uses **`serverUrl`** for a remote HTTP server,
> not `url` (same as Windsurf; different from Cursor and VS Code).

> **New tool:** Antigravity is recent and its config path has moved between
> releases. The path below matches the current docs; if yours differs, open the
> file from inside the app (see below) and apply the same JSON.

## 1. Add the server

`scripts/install_clients.py --only antigravity` writes this for you. To do it by
hand, edit `~/.gemini/config/mcp_config.json`, or open it from the app:
**Settings → Customizations → Open MCP Config**.

```jsonc
{
  "mcpServers": {
    "afair": {
      "serverUrl": "<your-vault-url>/mcp",
      "headers": {
        "Authorization": "Bearer <AFAIR_AUTH_TOKEN>"
      }
    }
  }
}
```

For a **local self-host** there is no token; drop the `headers` block. For a
**public deployment** with OAuth, also drop `headers`.

## 2. Add the instruction snippet

Add the snippet from [_snippet.md](_snippet.md) to a repo-root `AGENTS.md` so the
agent reaches for the afair tools on its own.

## 3. Verify

Open **Manage MCP Servers** from the agent panel, confirm afair is connected,
then:

> Use the afair MCP server to list its tools.

Expected: `remember`, `recall`, `observe`. Then a cross-session round-trip:

> Use afair to remember: "Antigravity verification, the cross-vendor stack works"

## Troubleshooting

- **Server won't connect:** `url` instead of `serverUrl`, or the config in the
  wrong file. Use **Open MCP Config** from the app to be sure you are editing the
  file Antigravity actually reads.
- **Local URL rejected:** a local self-host runs without auth, drop `headers`.
