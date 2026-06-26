# Codex CLI (OpenAI) — connecting to afair

Codex CLI configuration lives at `~/.codex/config.toml`.

## 1. Add the server

Append to `~/.codex/config.toml`:

```toml
[mcp_servers.afair]
type = "http"
url = "<your-vault-url>/mcp"

[mcp_servers.afair.headers]
Authorization = "Bearer <AFAIR_AUTH_TOKEN>"
```

Replace `<AFAIR_AUTH_TOKEN>` with the value from your `.env.local`.

## 2. Add the instruction snippet

Append the contents of [_snippet.md](_snippet.md) to `~/.codex/AGENTS.md`
(Codex's persistent-instructions file, the equivalent of CLAUDE.md).

## 3. Verify

Restart Codex (or run `codex mcp restart afair`). Then in a new
session:

> Use the afair MCP server to list the tools available.

Expected: three tools — `remember`, `recall`, `observe`.

Round-trip test — first save, then recall in a new session:

> Use afair to remember: "Codex CLI verification 2026-05-25"

Then in a fresh `codex` session:

> Recall what we noted about 2026-05-25 from Codex.

Should return the fact across sessions and clients (e.g., you can save
from Codex and recall from Claude Code — that proves the cross-vendor
invariant I5).

## Troubleshooting

### TOML format issues

Codex's MCP config differs slightly from Claude Code's. The headers
section is a nested TOML table, not inline. If you copy-paste an example
that uses inline-table notation `{Authorization = "..."}`, some Codex
versions reject it — use the nested form above.

### Codex doesn't see the tools

```bash
codex mcp list
codex mcp logs afair
```

If `mcp logs` shows 401 responses, the token is wrong. Same verification
as in [claude-code.md](claude-code.md) — curl the endpoint directly with
your token and check for `307` (auth passed) vs `401` (auth failed).

### Codex's permission model

By default Codex prompts before each MCP tool call. To allow `recall`
without prompting (it's a read; safe), add the tool to your auto-allow
list per Codex's docs. `remember` and `observe` modify the vault — prompt
discipline matters.
