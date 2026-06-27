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
client at it (Claude Code, Codex, Cursor; see [docs/clients](clients)) and you
are done. Locally you need no auth and no encryption key. Web clients
(Claude.ai, ChatGPT) are a separate case: they run in the vendor's cloud, so a
local server cannot serve them (see [Connecting clients](#connecting-clients-cli-vs-web)
below).

## What it stores, and where

Everything lives under `VAULT_DIR` (default `~/vault`): the append-only SQLite
database plus a filesystem object store for large/binary content. Back up that
one directory and you have backed up the whole vault. There is no external
database, queue, or cache to operate.

## Production checklist

When you expose afair on the public internet, two settings become mandatory and
the server refuses to boot without them (`ENVIRONMENT=fly`):

- **`AFAIR_AUTH_TOKEN`** — a bearer token every MCP client must send. Without it
  the substrate would be world-readable and world-writable.
- **`AFAIR_VAULT_KEY`** — the vault encryption key. With it set, the SQLite
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
and talk to the server directly. They send the static `AFAIR_AUTH_TOKEN` as a
bearer and need no OAuth. This works against a local `127.0.0.1` server and a
public one alike. Nothing else in this section applies to them.

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
- **Keep a copy somewhere safe and separate** from the server — a password
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
