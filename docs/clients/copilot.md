# GitHub Copilot: connecting to afair

GitHub Copilot reaches MCP servers through **VS Code's agent mode** (VS Code
1.102+, Copilot Chat in *Agent* mode). The server is configured in a
user-level `mcp.json`; Copilot's *instructions* are per-repository.

> Format note: VS Code's `mcp.json` uses a top-level **`servers`** key (not
> `mcpServers` like Cursor), and a remote server uses `"type": "http"`.

## 1. Add the server

The installer writes this for you (`scripts/install_clients.py`). To do it by
hand, create or edit the VS Code **user** `mcp.json`:

- macOS: `~/Library/Application Support/Code/User/mcp.json`
- Linux: `~/.config/Code/User/mcp.json`
- Windows: `%APPDATA%\Code\User\mcp.json`

```jsonc
{
  "servers": {
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

For a **local self-host** there is no token; drop the `headers` block. For a
**public deployment** with OAuth, also drop `headers`: VS Code runs the browser
sign-in on first use.

Per-project instead of global? Put the same JSON in `<project>/.vscode/mcp.json`.

## 2. Turn on agent mode + the tools

1. Open **Copilot Chat** in VS Code.
2. Switch the mode dropdown from *Ask* / *Edit* to **Agent**.
3. Open the tools picker (the wrench/tools icon) and enable the **afair**
   server's tools. MCP tools are only callable from agent mode.

## 3. Add the instruction snippet (per repo)

Copilot reads instructions per workspace, so there is no single global file.
Add the snippet to one of:

- `.github/copilot-instructions.md` at the repo root (Copilot's native file), or
- a repo-root `AGENTS.md` (VS Code 1.104+ reads it when the *Use AGENTS.md*
  setting is on).

Paste the contents of [_snippet.md](_snippet.md). This is what makes Copilot
reach for `recall` / `remember` / `observe` on its own instead of only when you
ask.

## 4. Verify

In an **agent-mode** Copilot chat:

> Use the afair MCP server to list its tools.

Expected: `remember`, `recall`, `observe`. Then a cross-session round-trip:

> Use afair to remember: "Copilot verification, the cross-vendor stack works"

Open a new chat (or another client) and ask what was saved. The fact comes
back from the shared vault.

## Troubleshooting

### The afair tools don't appear

- You're in *Ask* or *Edit* mode. MCP tools are **agent-mode only**.
- The server is in the wrong file. VS Code uses `mcp.json` with the `servers`
  key, not `settings.json` and not `mcpServers`.
- Reload the window (Command Palette → *Developer: Reload Window*) after editing
  `mcp.json`.

### Copilot has the tools but never calls them

It has no standing instruction to. Add the snippet from step 3 to the repo's
`.github/copilot-instructions.md`. Without it, the tools are available but the
model only uses them when you explicitly ask.

### "I only have an OAuth login, no API key"

That connects Copilot to afair fine, the MCP layer needs no LLM key. But afair's
*automatic structuring* (the cold-path agents that extract entities and decide
salience) needs its **own** small LLM key, independent of whatever Copilot signs
in with. Without one, afair still stores and recalls everything verbatim; it
just doesn't auto-organize. Set the provider key that matches `EXTRACTOR_MODEL`
(the default `anthropic/claude-haiku-4-5` uses `ANTHROPIC_API_KEY`; it's cheap)
and keep `COLD_PATH_ENABLED=true`. See [self-hosting.md](../self-hosting.md).
