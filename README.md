<p align="center">
  <img src="assets/logo/afair-elephant.png" alt="afair" width="160" />
</p>

<h1 align="center">afair</h1>

<p align="center">
  <em>Your memory across every AI you use.</em>
</p>

<p align="center">
  <a href="https://github.com/afairai/afair/actions/workflows/ci.yml"><img src="https://github.com/afairai/afair/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL_v3-blue.svg" alt="License: AGPL v3" /></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.12%2B-blue.svg" alt="Python 3.12+" /></a>
  <a href="https://modelcontextprotocol.io"><img src="https://img.shields.io/badge/MCP-Model_Context_Protocol-7c3aed.svg" alt="MCP" /></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/badge/lint-ruff-261230.svg" alt="Ruff" /></a>
</p>

---

You use more than one AI, and each of them remembers you separately. What you
build up in one tool doesn't exist in the next, so you keep re-explaining
yourself: your decisions, the people in your life, the preferences and the
history you're tired of repeating at the start of every session.

afair is one memory that all of them share. It runs as your own vault and
connects to each tool through the [Model Context
Protocol](https://modelcontextprotocol.io) (MCP), so the same memory follows you
from Claude to your coding agent to whatever you pick up next.

Capturing a thought is cheap. Keeping it structured, the people, the projects,
what changed, what still matters, is the expensive part, and it stays expensive
because your life keeps moving. That work is interpretation: reasoning, not
filing, and it's exactly what these models have gotten good at. So afair does it
for you. An AI layer reads what you've told it and keeps the structure current on
its own, so you never file or maintain anything. You can see what it has learned
about you, and correct it when it's wrong.

What goes in is your whole life: your work, the people you love, and the
personal things you'd rather an AI just knew.

The data is append-only and yours. afair is open source and single-tenant, and
you can export all of it whenever you want. The name is short for "as far as I
remember", the hedge it's built to make unnecessary.

## Two ways to use it

**Run it yourself.** This repository is the whole thing, AGPLv3. Self-host it on
your own machine or server and you own every layer end to end. It is free,
forever. The quickstart is below.

**Or let afair.ai run it for you.** Managed hosting, with your own isolated EU
instance, backups, export, and updates handled for you, is coming soon. Join the
early-access list at **[afair.ai](https://afair.ai)**.

Same code either way. The hosted product is one deployment of this repo, not a
separate proprietary fork.

## The three commands

afair exposes exactly three tools, and they are frozen for good:

- **`remember`** stores something durable: a decision, a person who matters, a
  date you can't miss, a preference.
- **`recall`** pulls back what is relevant to the moment.
- **`observe`** logs what the AI just did, so the vault keeps up.

Once you hand your AI the short [setup snippet](docs/clients/_snippet.md), it
calls these on its own. Nothing reaches the vault unless a call puts it there.

## Run it yourself

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env
# add ANTHROPIC_API_KEY, or a key for any other provider (the model is yours to pick)
uv run python -m afair
```

The server comes up on `http://127.0.0.1:8765`. Point a CLI or desktop client
(Claude Code, Codex, Cursor, GitHub Copilot) at it, connect, and you are done. Web clients that
run in the cloud (Claude.ai, ChatGPT) instead need a public HTTPS deployment and
a quick OAuth setup, covered in [docs/self-hosting.md](docs/self-hosting.md).
Every environment variable is documented inline in `.env.example`; per-client
connection guides live in [docs/clients](docs/clients).

## Install it with your coding agent

Already in Claude Code, Codex, or another coding agent? Hand it the prompt below
and it sets itself up: clone, dependencies, config, and the MCP wiring, then it
proves the round-trip.

> Set up afair (the open-source MCP memory server at
> https://github.com/afairai/afair) on this machine and connect this tool to it.
> Clone it, run `uv sync`, copy `.env.example` to `.env`, and ask me for an LLM
> provider API key to put there. Then run
> `uv run python scripts/install_clients.py` to wire my MCP clients and add the
> afair instruction snippet. Tell me the command to start the server
> (`uv run python -m afair`) and how to keep it running. Once it is up, remember
> a test fact and recall it to prove it works. Follow `docs/self-hosting.md` and
> `docs/clients/`; put my API key only in `.env`, nowhere else.

That runs locally and serves CLI and desktop clients out of the box. To reach
web clients (Claude.ai, ChatGPT), follow the public-deployment notes in
[docs/self-hosting.md](docs/self-hosting.md).

## Teach your AI to use it

Connecting the server is half of it; the other half is making your AI reach for
the tools on its own. Paste the short [instruction snippet](docs/clients/_snippet.md)
into the client's persistent instructions (CLAUDE.md, AGENTS.md, Custom
Instructions, or `.cursorrules`), and it will recall context at the start of a
conversation, remember what's durable, and observe what it does, without you
prompting it each time. The same snippet works for every client. For Claude
Code, Codex, and Cursor, `scripts/install_clients.py` writes both the connection
config and the snippet for you; for GitHub Copilot it writes the connection
config and prints the one per-repo snippet step (Copilot reads instructions per
workspace).

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

## Documentation

| Doc | What's in it |
|---|---|
| [VISION.md](VISION.md) | The full design and the eight invariants. Start here for the why. |
| [docs/self-hosting.md](docs/self-hosting.md) | Run your own vault: local, Docker, or a public deployment, with the CLI-vs-web client and OAuth setup. |
| [docs/clients](docs/clients) | Per-client connection guides (Claude Code, Codex, Cursor, GitHub Copilot, Claude.ai, ChatGPT, Perplexity) and the one universal instruction snippet. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Dev setup, the four checks, and the invariants a change cannot break. |
| [SECURITY.md](SECURITY.md) | How to report a vulnerability, and the security model to hold afair against. |
| [docs/adr](docs/adr) | Architecture Decision Records: why the invariants exist, why the entity graph is a belief layer. |
| [CHANGELOG.md](CHANGELOG.md) | Release history. |

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
