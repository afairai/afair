# Self-hosting afair

afair is a single Python process backed by a SQLite file. You can run it on
your laptop, a VPS, a Raspberry Pi, or any container host. This guide covers a
local run and a production deployment to [Fly.io](https://fly.io) (what the
hosted afair.ai uses), but nothing here is Fly-specific: it's one container with
one persistent volume.

If you would rather not run anything, [afair.ai](https://afair.ai) hosts it for
you, in the EU, with backups and export handled.

## Local

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/afairai/afair.git
cd afair
uv sync
cp .env.example .env
# add ANTHROPIC_API_KEY (or any provider key); the rest has working defaults
uv run python -m afair
```

The server listens on `http://127.0.0.1:8765`. Point a CLI or desktop MCP
client at it (Claude Code, Codex, Cursor, GitHub Copilot; see
[docs/clients](clients)) and you are done. Locally you need no auth and no
encryption key. Web clients (Claude.ai, ChatGPT) are a separate case: they run
in the vendor's cloud, so a local server cannot serve them (see
[Connecting clients](#connecting-clients-cli-vs-web) below).

## How many API keys do I need?

**One is enough.** afair needs its own LLM key for the structuring layer (the
cold-path agents that extract entities and decide salience), independent of
whatever your coding agent signs in with. The MCP connection itself needs no key
at all; without an LLM key afair still stores and recalls everything verbatim,
it just doesn't auto-organize.

afair has four model roles. Each points at a litellm model via a `*_MODEL`
variable, and you only need keys for the providers those models actually use:

| Role | Default model | Provider key | Needed when |
|---|---|---|---|
| `EXTRACTOR_MODEL` (text structuring) | `anthropic/claude-haiku-4-5` | `ANTHROPIC_API_KEY` | the core "organizes itself" |
| `VISION_MODEL` (images) | `anthropic/claude-haiku-4-5` | `ANTHROPIC_API_KEY` (same) | only if you remember images |
| `EMBEDDING_MODEL` (semantic recall) | `openai/text-embedding-3-small` | `OPENAI_API_KEY` | only if `SEMANTIC_RECALL_ENABLED=true` |
| `TRANSCRIPTION_MODEL` (audio) | `openai/whisper-1` | `OPENAI_API_KEY` (same) | only if you remember audio |

**With the defaults: two keys** (Anthropic for extraction + vision, OpenAI for
embeddings + transcription). The one OpenAI key covers both OpenAI roles, and the
one Anthropic key covers both Anthropic roles. This split is the quality default,
not a requirement.

**To run on a single key**, point every role at one provider:

- **All OpenAI (simplest):** set `EXTRACTOR_MODEL` and `VISION_MODEL` to
  `openai/gpt-4o-mini`. One `OPENAI_API_KEY` then covers text, vision, embeddings,
  and audio, the whole product.
- **All Anthropic:** one `ANTHROPIC_API_KEY` covers text + vision, but Anthropic
  ships no embeddings, so set `SEMANTIC_RECALL_ENABLED=false` (recall falls back
  to FTS/keyword) and skip audio.

If your harness logs in with OAuth and you have no standalone API key, the
structuring stays off until you give afair a model it can reach. Two good
keyless options: if you pay for **GitHub Copilot**, point afair at it and reuse
that login (see [Use your GitHub Copilot subscription](#use-your-github-copilot-subscription-no-api-key)),
or run the structuring **fully local** with Ollama (see
[Run it fully local](#run-it-fully-local-no-external-provider)). Otherwise the
cheapest hosted path is a single OpenAI key with every role pointed at OpenAI.

## Run it fully local (no external provider)

afair can run with **zero external calls**, nothing leaves your machine. This is
I4 (you own the substrate) taken all the way: no vendor ever sees a byte. It is a
self-host / homelab path, not the hosted default, because of one resource cost
explained below.

afair has three LLM needs, and only one is heavy:

| Need | Local path | Cost |
|---|---|---|
| **Recall** (the hot path) | FTS5 + sqlite-vec, always local | negligible |
| **Embeddings** (semantic recall) | `fastembed/<model>` (ONNX, in-process, no network; already a dependency) | light: CPU, ~100–400 MB, runs on any node |
| **Structuring** (the extractor) | `ollama/<model>` via litellm | this is the part that wants RAM / a GPU |

Recall and embeddings are light enough for a small box. The only component that
benefits from a bigger node is the extractor LLM, and because structuring runs on
the **cold path** (background, not in the recall response), it tolerates a slow
CPU: a few seconds per event is fine since it never blocks a recall.

**Hardware, extractor only:**

- A **7–8B** model (e.g. `qwen2.5:7b`, `llama3.1:8b`, q4 quant) needs ~6–8 GB RAM.
  Comfortable on a 16 GB machine. Apple Silicon (M-series) or any GPU with ≥8 GB
  VRAM is ideal; CPU-only works but is slow.
- A **14–32B** model structures noticeably better and needs ~16–32 GB, GPU
  recommended.

A 16 GB Mac mini or a homelab box with a consumer GPU runs the whole thing
comfortably. A 1 GB VPS cannot host a useful local LLM, that is the real
"needs a bigger node," and it applies to the extractor alone.

**Config (text + structuring + recall, fully local):**

```bash
# Structuring on a local Ollama model — no provider key
EXTRACTOR_MODEL=ollama/qwen2.5:7b
# Embeddings via local ONNX — no provider key, no network
EMBEDDING_MODEL=fastembed/BAAI/bge-small-en-v1.5
EMBEDDING_DIM=384                 # must match the fastembed model
SEMANTIC_RECALL_ENABLED=true
COLD_PATH_ENABLED=true
# Leave ANTHROPIC_API_KEY / OPENAI_API_KEY unset.
```

Then run Ollama alongside afair:

```bash
ollama serve            # or the macOS app
ollama pull qwen2.5:7b  # one-time model download
```

**Honest gaps in fully-local mode:**

- **Audio.** The default `TRANSCRIPTION_MODEL=openai/whisper-1` is OpenAI; there is
  no first-class local Whisper route through litellm yet. Skip audio memories, or
  transcribe out of band, until this lands.
- **Images.** Set `VISION_MODEL` to a local vision model (e.g.
  `ollama/llama3.2-vision:11b`) if you remember images; it is heavier than the
  text model.
- **The self-improvement judge** is cross-vendor by design (Sonnet + GPT-5 +
  Gemini). Fully local means running the tuner with a single local judge or
  leaving it off; it is background-only, so daily use is unaffected.
- **Quality.** A local 7B structures measurably worse than `claude-haiku-4-5`. It
  works, but "organizes itself" is cleaner on the hosted models. Bump to a larger
  local model if your hardware allows.

## Use your GitHub Copilot subscription (no API key)

If you already pay for GitHub Copilot, afair can use it as the structuring LLM
with **no separate API key and no extra cost**. litellm has a `github_copilot`
provider that reuses your Copilot login, and afair is vendor-neutral, so it is
just a model string. This is the cleanest path for the many developers whose
harnesses authenticate by OAuth and who have no standalone API key.

```bash
EXTRACTOR_MODEL=github_copilot/gpt-4.1
VISION_MODEL=github_copilot/gpt-4.1          # only if you remember images
# Copilot serves chat only, not embeddings, so keep embeddings local:
EMBEDDING_MODEL=fastembed/BAAI/bge-small-en-v1.5
EMBEDDING_DIM=384
# Leave ANTHROPIC_API_KEY / OPENAI_API_KEY unset.
```

**One-time login.** On the first call litellm runs GitHub's device flow. afair
prints a line like:

```
Please visit https://github.com/login/device and enter code XXXX-XXXX to authenticate.
```

Open that URL, enter the code, approve. litellm caches the token under
`~/.config/litellm/github_copilot/`, and every later call (including the
background cold-path) reuses it headless. You authorize once.

**Gotchas:**

- **Embeddings.** A `github_copilot/*` embedding model returns *"Model is not
  supported"*, Copilot exposes chat models only. Use local `fastembed` for
  embeddings (above), or an `openai/*` embedding model with an OpenAI key.
- **Model names** follow Copilot's catalogue (`github_copilot/gpt-4.1`,
  `github_copilot/gpt-4o`, `github_copilot/claude-sonnet-4`, ...). Pick one your
  Copilot plan includes.
- The same trick works for **any litellm provider**: set the model string and
  either let litellm read that provider's standard env var (`GROQ_API_KEY`,
  `MISTRAL_API_KEY`, `OPENROUTER_API_KEY`, ...) or, for self-authenticating ones
  like `github_copilot`, nothing at all.

## What it stores, and where

Everything lives under `VAULT_DIR` (default `~/vault`): the append-only SQLite
database plus a filesystem object store for large/binary content. Back up that
one directory and you have backed up the whole vault. There is no external
database, queue, or cache to operate.

## Production checklist

When you expose afair on the public internet, two settings become mandatory and
the server refuses to boot without them (`ENVIRONMENT=fly`):

- **`AFAIR_AUTH_TOKEN`**: a bearer token every MCP client must send. Without it
  the substrate would be world-readable and world-writable.
- **`AFAIR_VAULT_KEY`**: the vault encryption key. With it set, the SQLite
  database is opened through SQLCipher (whole-file AES-256) and blobs are written
  with AES-256-GCM. **Losing this key after data has been written under it means
  the data is unrecoverable.** Keep a copy somewhere safe and separate.

Generate either with:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Every other variable is documented inline in [`.env.example`](../.env.example).
The OAuth variables (section 7) are needed only to serve web clients; the
managed-fleet variables (section 8) are not used by a self-host. For CLI and
desktop clients the static bearer token is all you need.

## Connecting clients: CLI vs web

How a client authenticates depends on whether it runs on your machine or in
someone else's cloud.

**CLI and desktop clients** (Claude Code, Codex, Cursor, Windsurf) run locally
and talk to the server directly. They can send the static `AFAIR_AUTH_TOKEN` as
a bearer (the simplest path, and the only one against a local loopback server),
or do the OAuth browser sign-in when they connect to a public deployment. With
the bearer path, nothing else in this section applies to them.

**Web clients** (Claude.ai, ChatGPT) run in the vendor's cloud. They cannot
reach `localhost` and cannot send a bearer token, so they need two things a
local run does not provide: a public HTTPS URL, and a browser OAuth login. To
serve them from your own instance:

1. Deploy the server somewhere publicly reachable over HTTPS (see *Deploy to
   Fly.io* below, or any container host), and set `OAUTH_ISSUER` to that public
   URL, for example `https://memory.example.com`.
2. Register a GitHub OAuth app at
   <https://github.com/settings/developers> (*New OAuth App*). Set the
   **Authorization callback URL** to
   `https://<your-host>/oauth/identity/github/callback`. It is free and takes a
   minute.
3. Set these secrets on the deployment:

   ```bash
   IDENTITY_BACKEND=github
   GITHUB_OAUTH_CLIENT_ID=<from the GitHub app>
   GITHUB_OAUTH_CLIENT_SECRET=<from the GitHub app>
   IDENTITY_ALLOWLIST=<your GitHub username>   # single-tenant: only you
   OAUTH_ISSUER=https://<your-host>
   AFAIR_JWT_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
   ```

The server runs the whole OAuth dance itself; GitHub is only the login, and
there is no dependency on afair.ai. (The `hub` identity backend is the reverse:
it federates login through afair.ai's control plane and exists for the managed
fleet, not for self-hosting.)

If you only use CLI and desktop clients, skip all of this and run with just the
bearer token, locally or in production.

## Run with Docker (any host)

The repo ships a `Dockerfile`. The same image runs locally, on a VPS, or on any
container host; Fly (below) is just one place to put it.

```bash
docker build -t afair .

docker run -d --name afair \
  -p 8080:8080 \
  -v afair-vault:/data \
  -e AFAIR_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  -e AFAIR_VAULT_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  -e OAUTH_ISSUER="https://your-host" \
  -e ANTHROPIC_API_KEY="sk-ant-..." \
  afair
```

The image defaults to `ENVIRONMENT=fly` (production mode): it enforces the auth
token and the vault key, and requires `OAUTH_ISSUER` (your public URL) at boot,
even if you only use CLI clients. The vault lives on the `afair-vault` volume at
`/data/vault`; back that volume up and you have the whole vault. The container
listens on port `8080`; put a TLS-terminating reverse proxy (Caddy, nginx,
Traefik) in front for the public HTTPS URL that web clients require.

For a throwaway local container with none of the production requirements, drop
into local mode:

```bash
docker run --rm -p 8765:8765 \
  -e ENVIRONMENT=local -e MCP_PORT=8765 \
  -e ANTHROPIC_API_KEY="sk-ant-..." \
  afair
```

### docker compose

```yaml
# compose.yaml: set the four values in a sibling .env file
services:
  afair:
    build: .
    ports:
      - "8080:8080"
    volumes:
      - afair-vault:/data
    environment:
      AFAIR_AUTH_TOKEN: ${AFAIR_AUTH_TOKEN}
      AFAIR_VAULT_KEY: ${AFAIR_VAULT_KEY}
      OAUTH_ISSUER: ${OAUTH_ISSUER}
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
    restart: unless-stopped
volumes:
  afair-vault:
```

Then `docker compose up -d`.

## Deploy to Fly.io

`fly.toml.example` is a starting config (rename it to `fly.toml` and set your own
app name). The shape: one machine, one volume mounted at `/data`, a `/health`
check, and the vault at `/data/vault`.

```bash
fly launch --no-deploy --copy-config --name <your-app>   # or edit fly.toml.example → fly.toml
fly volumes create vault --size 1 --region <your-region>
fly secrets set \
  ENVIRONMENT=fly \
  AFAIR_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  AFAIR_VAULT_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  ANTHROPIC_API_KEY=... \
  --app <your-app>
fly deploy --app <your-app>
```

`scripts/check_secrets.py <your-app>` verifies the boot-required secrets are set
before you deploy. After deploy, `curl https://<your-app>.fly.dev/health` should
return `200`.

## Verify it works

After any of the above, confirm the server is up:

```bash
curl -s <your-host>/health
# {"status":"ok"}
```

`<your-host>` is `http://127.0.0.1:8765` locally, or your public URL once
deployed. For a full no-client smoke (health + auth gate), run `scripts/smoke.sh`
from the repo root.

Then connect a client (see [docs/clients](clients)) and do the round-trip that
proves memory persists across tools. Ask the client to:

1. **remember** something: *"Use afair to remember: my deploy smoke test ran
   today."*
2. **recall** it, ideally from a different client: *"What did afair record about
   my deploy smoke test?"*

If the second client returns what the first one stored, the vault is live and
shared across your tools. That cross-tool round-trip is the whole point.

## Backups

Back up `VAULT_DIR`. On Fly, snapshot the volume (`fly volumes snapshots create`)
or run a periodic copy off-box. The vault is a single SQLite file plus a blob
directory, so any file-level backup tool works. Because the data is encrypted at
rest with `AFAIR_VAULT_KEY`, a stolen snapshot is useless without the key.

## Encryption and the vault key

When `AFAIR_VAULT_KEY` is set, the whole vault is encrypted at rest: the SQLite
database (including the FTS index) is opened through SQLCipher with a key derived
via HKDF, and each filesystem blob is sealed with AES-256-GCM under a separate
derived sub-key. Plaintext exists only in process memory and in what you send to
your LLM provider over the wire.

The key is **one-shot per vault**: once data has been written under it, that key
is the only thing that can read the vault. There is no recovery path and no
in-place re-keying, so:

- Generate it with `python -c 'import secrets; print(secrets.token_urlsafe(32))'`.
- Set it as a deployment secret (for example `fly secrets set AFAIR_VAULT_KEY=...`).
- **Keep a copy somewhere safe and separate** from the server: a password
  manager or an offline note. That copy is your only recovery path; lose the key
  and the data is gone.

To rotate the key there is no in-place re-key: provision a fresh vault under a
new key and re-import through the MCP export surface. The current design encrypts
the whole database file; finer-grained designs (per-event encryption, bring-your-
own-key, TEE) are future work.

## Upgrading

Pull, re-sync, redeploy. The substrate is append-only and forward-compatible by
design (Invariant I3): a newer build reads an older vault and re-interprets it;
it never migrates or rewrites stored events. The three MCP verbs are frozen
(I1), so your clients keep working across upgrades.
