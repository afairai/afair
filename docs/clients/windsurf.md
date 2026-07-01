# Windsurf: connecting to afair

Windsurf (Codeium's editor) reads MCP servers for Cascade from a JSON config.

> **Field gotcha:** Windsurf uses **`serverUrl`** for a remote HTTP server, not
> `url`. This trips up configs copied from Cursor or Claude. (Windsurf also
> accepts `url`, but `serverUrl` is the documented field.)

## 1. Add the server

`scripts/install_clients.py --only windsurf` writes this for you. To do it by
hand, edit `~/.codeium/windsurf/mcp_config.json`:

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

Prefer not to hardcode the token? Windsurf supports interpolation in
`serverUrl`, `headers`, and `env`: use `${env:AFAIR_AUTH_TOKEN}` and set the var
in your shell.

Or via the UI: **Windsurf → Settings → Cascade → MCP Servers → Add**, then paste
the JSON.

## 2. Add the instruction snippet

Add the snippet from [_snippet.md](_snippet.md) to Windsurf's global rules:
**Settings → Rules** (global). That makes Cascade reach for the afair tools on
its own.

## 3. Verify

Open the Cascade panel, confirm the afair server shows connected, then:

> Use the afair MCP server to list its tools.

Expected: `remember`, `recall`, `observe`. Then a cross-session round-trip:

> Use afair to remember: "Windsurf verification, the cross-vendor stack works"

## Troubleshooting

- **Server won't connect:** almost always `url` instead of `serverUrl`.
- **Reload after editing:** Windsurf caches MCP config; refresh the MCP panel (or
  restart) after editing `mcp_config.json`.
- **Local URL rejected:** a local self-host runs without auth, drop `headers`.
