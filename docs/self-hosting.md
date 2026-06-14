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

The server listens on `http://127.0.0.1:8765`. Point any MCP client at it (see
[docs/clients](clients)). Locally you need no auth and no encryption key; both
are required only when `ENVIRONMENT=fly` (see below).

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
The OAuth and hosted-deployment variables are optional for a single-user
self-host; you can ignore that whole block and use the static bearer token.

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

## Upgrading

Pull, re-sync, redeploy. The substrate is append-only and forward-compatible by
design (Invariant I3): a newer build reads an older vault and re-interprets it;
it never migrates or rewrites stored events. The three MCP verbs are frozen
(I1), so your clients keep working across upgrades.
