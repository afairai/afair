<p align="center">
  <img src="assets/logo/afair-elephant.png" alt="afair" width="160" />
</p>

<h1 align="center">afair</h1>

<p align="center">
  <em>Your memory across every AI you use.</em>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL_v3-blue.svg" alt="License: AGPL v3" /></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.12%2B-blue.svg" alt="Python 3.12+" /></a>
  <a href="https://modelcontextprotocol.io"><img src="https://img.shields.io/badge/MCP-Model_Context_Protocol-7c3aed.svg" alt="MCP" /></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/badge/lint-ruff-261230.svg" alt="Ruff" /></a>
</p>

---

afair is a memory vault for AI assistants. Connect it once, and the tools you
already use (Claude, ChatGPT, your coding agent, whatever you pick up next) can
remember what matters to you: your decisions, your projects, the context you are
tired of repeating at the start of every session.

It speaks the [Model Context Protocol](https://modelcontextprotocol.io) (MCP), so
any MCP client reaches the same vault. The data is append-only and yours, and you
can export all of it whenever you want.

## Two ways to use it

**Let afair.ai run it for you.** Sign up at **[afair.ai](https://afair.ai)** and
you get your own isolated instance, hosted in the EU, with backups, one-click
export, and updates handled for you. Nothing to install, no database to operate,
no keys to babysit. This is the right choice for most people.

**Or run it yourself.** This repository is the whole thing, AGPLv3. Self-host it
on your own machine or server and you own every layer end to end. The quickstart
is below.

Same code either way. The hosted product is one deployment of this repo, not a
separate proprietary fork.

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
`.env.example`. Per-client connection guides live in [docs/clients](docs/clients).

## Works with

Claude Code, Claude.ai, ChatGPT, Codex CLI, Cursor, Windsurf, Copilot, and
anything else that speaks MCP over Streamable HTTP.

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

## Contributing

Pull requests are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) for setup, the
checks that must pass, and the invariants a change cannot break. Found a security
issue? See [SECURITY.md](SECURITY.md), and please report it privately.

## License

afair is released under the GNU Affero General Public License v3.0
([LICENSE](LICENSE)). You can self-host it, fork it, and modify it freely. If you
run a modified version as a network service for others, you publish your changes
under the same license. The hosted offering at [afair.ai](https://afair.ai) is one
deployment of this code, not a separate proprietary fork.

In one line: free to use, free to host yourself, share back if you run it as a
service for others.

## Made in Germany

Built in Germany. The hosted instances run in the EU, under EU jurisdiction.
