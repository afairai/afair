<p align="center">
  <img src="assets/logo/afair-elephant.png" alt="afair" width="160" />
</p>

<h1 align="center">afair</h1>

<p align="center">
  <em>Your memory across every AI you use.</em>
</p>

---

afair is a memory vault for AI assistants. Connect it once, and the tools you
already use (Claude, ChatGPT, your coding agent, whatever you pick up next) can
remember what matters to you: your decisions, your projects, the context you are
tired of repeating at the start of every session.

It speaks the [Model Context Protocol](https://modelcontextprotocol.io) (MCP), so
any MCP client reaches the same vault. The data is append-only and yours. Export
all of it whenever you want, and run the whole thing yourself if you would rather.

## Works with

Claude Code, Claude.ai, ChatGPT, Codex CLI, Cursor, Windsurf, Copilot, and
anything else that speaks MCP over Streamable HTTP.

## The three commands

afair exposes exactly three tools, and they are frozen for good:

- **`remember`** stores something durable: a decision, a fact, a preference.
- **`recall`** pulls back what is relevant to the moment.
- **`observe`** logs what the AI just did, so the vault keeps up.

Once you hand your AI the short setup snippet, it calls these on its own. Nothing
reaches the vault unless a call puts it there.

## Run it yourself

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env
# add ANTHROPIC_API_KEY, or a key for any other provider (the model is yours to pick)
uv run python -m afair
```

The server comes up on `http://127.0.0.1:8765`. Point your MCP client at it,
connect, and you are done. Every environment variable is documented inline in
`.env.example`.

## Hosted option

Prefer not to run anything? [afair.ai](https://afair.ai) hosts it for you: your
own isolated instance, operated for you, with your data in the EU.

## Architecture

Four layers, one source of truth:

- **Substrate.** Append-only SQLite with FTS5 and sqlite-vec, content-addressed.
  The log is never rewritten.
- **Interpretation.** Versioned views built over the substrate. Regenerate them
  without touching a single stored event.
- **MCP surface.** Versioned and additive. A signature that has shipped keeps
  working.
- **Agents.** Background workers that extract, link entities, and synthesize, all
  reading the same substrate.

The complete design, and the eight invariants that hold it together, live in
[VISION.md](VISION.md). Start there if you want the why.

## License

afair is released under the GNU Affero General Public License v3.0
([LICENSE](LICENSE)). You can self-host it, fork it, and modify it freely. If you
run a modified version as a network service for others, you publish your changes
under the same license. The hosted offering at afair.ai is one deployment of this
code, not a separate proprietary fork.

In one line: free to use, free to host yourself, share back if you run it as a
service for others.

## Made in Germany

Built in Germany. The hosted instances run in the EU, under EU jurisdiction.
