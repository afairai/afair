# GitHub Copilot CLI: connecting to afair

The GitHub Copilot CLI (the `copilot` terminal agent, separate from Copilot in
VS Code) reads MCP servers from its own config file.

> This is a different surface from [Copilot in VS Code](copilot.md). The CLI has
> its own config file; setting up one does not set up the other.

## 1. Add the server

`scripts/install_clients.py --only copilot-cli` writes this for you. To do it by
hand, edit `~/.copilot/mcp-config.json` (the directory can be moved with the
`COPILOT_HOME` env var):

```jsonc
{
  "mcpServers": {
    "afair": {
      "type": "http",
      "url": "<your-vault-url>/mcp",
      "headers": {
        "Authorization": "Bearer <AFAIR_AUTH_TOKEN>"
      },
      "tools": ["*"]
    }
  }
}
```

For a **local self-host** there is no token; drop the `headers` block. For a
**public deployment** with OAuth, also drop `headers` and the CLI runs the
browser sign-in on first use. `"tools": ["*"]` enables all three afair tools.

## 2. Add the instruction snippet (per repo)

Copilot reads instructions per workspace. Add the snippet from
[_snippet.md](_snippet.md) to your repo's `.github/copilot-instructions.md` (or a
repo-root `AGENTS.md`) so the agent reaches for `recall` / `remember` /
`observe` on its own.

## 3. Verify

In a Copilot CLI session:

> Use the afair MCP server to list its tools.

Expected: `remember`, `recall`, `observe`. Then a cross-session round-trip:

> Use afair to remember: "Copilot CLI verification, the cross-vendor stack works"

Open a new session (or another client) and ask what was saved.

## Bonus: Copilot as afair's own LLM (no API key)

If you pay for Copilot, you can also point afair's *structuring* at your Copilot
subscription, no separate API key, via litellm's `github_copilot` provider. See
["Use your GitHub Copilot subscription"](../self-hosting.md#use-your-github-copilot-subscription-no-api-key)
in the self-hosting guide. That is about the server's LLM backend; the config
above is about connecting the CLI *client* to afair. They are independent.

## Troubleshooting

- **Tools don't appear:** the server is in the wrong file. The CLI uses
  `~/.copilot/mcp-config.json` with a top-level `mcpServers` key.
- **`malformed` error from the installer:** your existing `mcp-config.json` has a
  JSON syntax error; run it through `jq` to find it.
- **Auth fails against a local URL:** a local self-host has no token, remove the
  `headers` block entirely.
