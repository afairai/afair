# Gemini CLI: connecting to afair

Google's Gemini CLI reads MCP servers from its settings file.

> **Field gotcha:** Gemini CLI uses **`httpUrl`** for a Streamable HTTP server
> (afair's transport), not `url`. `url` is reserved for the legacy SSE
> transport. Copying a Cursor/Claude config verbatim will not connect.

## 1. Add the server

`scripts/install_clients.py --only gemini-cli` writes this for you. To do it by
hand, edit `~/.gemini/settings.json` (user scope) or `.gemini/settings.json`
(project scope):

```jsonc
{
  "mcpServers": {
    "afair": {
      "httpUrl": "<your-vault-url>/mcp",
      "headers": {
        "Authorization": "Bearer <AFAIR_AUTH_TOKEN>"
      }
    }
  }
}
```

For a **local self-host** there is no token; drop the `headers` block. For a
**public deployment** with OAuth, also drop `headers`.

You can also add it from the CLI, which supports `-H` for headers:

```bash
gemini mcp add --transport http afair <your-vault-url>/mcp \
  -H "Authorization: Bearer <AFAIR_AUTH_TOKEN>"
```

## 2. Add the instruction snippet

Gemini CLI reads project and user context from `GEMINI.md`. Paste the snippet
from [_snippet.md](_snippet.md) into `~/.gemini/GEMINI.md` (global) or a
project-root `GEMINI.md` so the model uses the afair tools on its own.

## 3. Verify

In the Gemini CLI, run `/mcp` to see connected servers, or ask:

> Use the afair MCP server to list its tools.

Expected: `remember`, `recall`, `observe`. Then a cross-session round-trip:

> Use afair to remember: "Gemini CLI verification, the cross-vendor stack works"

## Troubleshooting

- **Server shows as disconnected:** almost always `url` instead of `httpUrl`.
  afair speaks Streamable HTTP, so the field must be `httpUrl`.
- **`/mcp` lists no tools:** check the bearer token has no trailing whitespace or
  newline, and that the health endpoint responds (`curl <your-vault-url>/health`).
- **Local URL rejected:** a local self-host runs without auth, drop `headers`.
