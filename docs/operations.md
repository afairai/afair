# Operations — afair Phase 0

> **Status:** Living document. Update whenever a recipe changes in reality.
> **Audience:** the user and any future AI agent or contributor.

## 0. Current state

- **App:** `afair` on Fly (personal org)
- **Region:** `fra` (EU residency by default)
- **Volume:** `vault`, 1 GB
- **Backup:** Fly automatic daily volume snapshots, **14-day retention** (bumped from 5d on 2026-05-30). RPO ~24h. Future RPO upgrades documented in §7.
- **Strategy:** `immediate` — single-machine deploys with brief downtime
- **URL:** `https://afair.fly.dev` (HTTPS auto-provisioned), MCP at `https://mcp.afair.ai`
- **Deploy:** GitHub Actions on push to `main` (see `.github/workflows/deploy.yml`)

---

## 1. Deploy strategy — why `immediate`, not blue/green

True blue/green requires two machines running concurrently during the swap.
Phase 0 stores everything in **one SQLite file on one Fly volume**, which is
single-writer by design and single-machine-mount by Fly's volume model.

So we use `strategy = "immediate"`:

- Old machine stops
- New image swaps in **on the same machine**, same volume
- Machine restarts; traffic resumes

Brief downtime (~10–30 s) per deploy. The substrate is never forked, never
raced, never corrupted. The single volume stays attached to the single
machine throughout.

True blue/green becomes possible when **LiteFS** lands in Phase 8: with a
primary + replicas, the new image can serve from a replica while the old
primary still answers, then promote.

---

## 2. Routine deploy

### Via GitHub Actions (the standard path)

```bash
git push origin main
```

The workflow at `.github/workflows/deploy.yml`:

1. Checks out the code
2. Runs `ruff check` + `ruff format --check` + `mypy --strict` + `pytest`
3. Builds the image via Fly's remote builder (`--remote-only`)
4. Deploys with `--wait-timeout 5m`
5. Verifies `/health` returns 200

Watch progress: `gh run watch` or [github.com/gowry/afair/actions](https://github.com/gowry/afair/actions)

### When push-to-main does NOT auto-trigger CI

Observed 2026-05-26: GitHub Actions' push-event dispatcher silently
dropped a string of consecutive pushes — workflow runs never enqueued
even though the commits landed on `main`. Repo hooks are empty by
design (Actions uses GitHub's internal dispatcher, not user-visible
webhooks), so there's no way to verify delivery from the outside.

**Recovery steps (in order):**

1. Manual workflow dispatch — wakes the dispatcher up:
   ```bash
   gh workflow run deploy.yml --ref main
   gh run watch
   ```
   If this fails with HTTP 500, retry — it's typically transient.

2. If `gh workflow run` keeps failing, fall back to direct flyctl:
   ```bash
   flyctl deploy --app afair --remote-only --wait-timeout 300
   ```
   This bypasses CI entirely. Run the smoke against the deployed app
   afterwards (see §9) to confirm the deploy went through:
   ```bash
   URL=https://afair.fly.dev TOKEN=$(grep '^AFAIR_AUTH_TOKEN=' .env.local | cut -d= -f2-) \
     uv run python scripts/smoke_mcp.py
   ```

3. Document the recovery in `analysis/phase-0-journal.md` so the next
   pipeline incident has prior art.

### Manual fallback

If GH Actions is unavailable or you want to deploy from a non-main branch:

```bash
fly deploy --remote-only --wait-timeout 5m
```

`--remote-only` builds on Fly's builder, not your laptop. Faster, no local
Docker daemon needed, deterministic builds.

---

## 3. Data persistence — what survives what

| Scenario | Substrate outcome |
|---|---|
| `fly deploy` / `git push origin main` | ✅ Volume stays mounted on the same machine. Data intact. |
| Machine restart / Fly host reboot | ✅ Volume reattaches automatically. |
| `fly machine destroy <id>` then redeploy | ⚠️ Volume persists, reattach may need manual step. See §5. |
| Fly host hardware failure | ⚠️ Volume is single-host NVMe. Recover via §6 snapshot restore. |
| `fly volumes destroy <vol-id>` | ❌ Permanent deletion of that volume. |
| `fly apps destroy afair` | ❌ Everything gone — app, machine, volume, snapshots. |

---

## 4. Backup to laptop (the I4 escape hatch)

Per Invariant I4, the user owns the substrate. They must always be able
to extract it.

```bash
# Pull the whole vault to your laptop
mkdir -p ~/afair-backups/$(date +%Y-%m-%d)
cd ~/afair-backups/$(date +%Y-%m-%d)

fly ssh sftp shell -a afair <<'EOF'
get /data/vault/substrate.db ./substrate.db
get -r /data/vault/objects ./objects
EOF

# Verify with sqlite3 directly — this is the canonical inspection
sqlite3 ./substrate.db "SELECT COUNT(*) FROM events;"
```

The downloaded directory is a complete, self-contained vault. You can
point a local `afair` server at it (`VAULT_DIR=./` in `.env.local`)
and it works without Fly.

---

## 5. Reattach an orphaned volume to a new machine

If a machine got destroyed and the volume is now unattached:

```bash
fly volumes list -a afair
# Note the volume id (vol_...).

fly machine list -a afair
# If there's no machine, create one and attach the existing volume.

fly machine create \
  --app afair \
  --region fra \
  --vm-size shared-cpu-1x \
  --vm-memory 512 \
  --volume vol_xxxxx:/data \
  --image registry.fly.io/afair:latest
```

For routine use, just `fly deploy` again — Fly will spin up a machine
that reattaches the existing volume if config matches.

---

## 6. Restore from a snapshot

Fly takes automatic daily snapshots (5-day retention).

```bash
fly volumes snapshots list -a afair
# Lists snapshots with ids, dates, sizes.

# Create a NEW volume from a snapshot (does NOT overwrite the existing one)
fly volumes create vault-restored \
  --app afair \
  --region fra \
  --size 1 \
  --snapshot-id vs_xxxxx

# Either point the app at the restored volume by editing fly.toml mounts,
# or manually copy data over from a temporary machine that has both mounted.
```

For a quick "I broke the substrate, give me yesterday's" path:

```bash
# 1. Stop the app to release the current volume
fly scale count 0 -a afair

# 2. List snapshots, pick one
fly volumes snapshots list -a afair

# 3. Destroy the current vault and create a new one from the snapshot
#    (DESTRUCTIVE — make sure you've backed up to laptop per §4 first!)
fly volumes destroy <current-vol-id> -a afair
fly volumes create vault --region fra --size 1 --snapshot-id <snapshot-id>

# 4. Scale back up
fly scale count 1 -a afair
```

---

## 7. Backup strategy + RPO

**Current state:**
- Fly automatic **daily** volume snapshots, 14-day retention (the floor)
- GitHub Actions **hourly** snapshot cron at `:17` past each hour
  (`.github/workflows/hourly-backup.yml`)

RPO is ~1 h, RTO is minutes (snapshot restore + machine boot). The
hourly cron uses the same volume snapshot mechanism as Fly's automatic
daily — restore is identical (see §6).

The hourly workflow needs `FLY_API_TOKEN` as a GitHub repo secret
(already present for the deploy workflow). If you rebuild the volume
on a different ID, update `VAULT_VOLUME_ID` in the workflow env.

When ~1 h RPO is no longer enough, the next upgrade path is **LiteFS
Cloud** (RPO < 1 s, Fly-native, ~$10/db/month base) — see the
trade-off matrix below.

### Increase snapshot retention

Default after `fly volumes create … --snapshot-retention 5` is 5 days.
Bump to 14 with:

```bash
fly volumes list -a afair                # note volume id
fly volumes update <vol-id> -a afair --snapshot-retention 14
```

### List snapshots + restore

See §6.

### Upgrade paths beyond the current ~1h floor

Two realistic options, in increasing order of complexity:

**B — LiteFS Cloud** *(RPO < 1s, Fly-native)*

Fly's managed SQLite replication service. Designed for the
single-database-per-machine model. Single subscription, programmatic
namespace provisioning via Fly API → fits the multi-user invite flow
without external vendors. Cost ~$10/db/month base + storage.

Replaces Fly volume snapshots as the primary backup mechanism. Self-hosted
users get a different backup story (Litestream against their own S3, or
just manual volume snapshots).

**C — Litestream against an external S3-compat (e.g., Cloudflare R2)** *(RPO < 1s, vendor mixed)*

What we briefly trialed and rolled back on 2026-05-30. RPO competitive
with LiteFS Cloud, cheaper per GB, but adds a second vendor to the
trust chain AND duplicates user content into third-party storage
(failed the security audit: substrate plaintext + OAuth secrets leak
to R2 via continuous WAL streaming unless encryption + DB-split is
done correctly).

Not the default choice. If we ever revisit it, see commit `4f02cac`
(revert) and `08b17c6` (original Litestream wire-up) for the prior art.

### Trade-off matrix

| Option | RPO | Vendors | Per-user provisioning cost | Self-host story |
|---|---|---|---|---|
| ~~Daily snapshots only~~ | ~24h | Fly only | zero | Volume snapshots |
| **Current — daily + hourly cron** | ~1h | Fly only | zero (GH Actions runs once) | Same; users can copy the workflow |
| B — LiteFS Cloud | <1s | Fly only | one API call per user namespace | User runs own LiteFS or volume snapshots |
| C — Litestream → external S3 | <1s | Fly + S3 vendor | bucket + token per user | User runs Litestream to their own S3 |

### Decision: hourly snapshots are live; LiteFS when invites scale

Phase 0 = me, one machine, one vault, daily-use validation. 1h RPO
costs me at most one hour of memories if Fly's NVMe craters.

When ~1 h still hurts (probably triggered by the first user who pays
a subscription fee), bump to LiteFS Cloud (Option B). The
trade-off matrix above captures why C (Litestream → external S3) is
the avoided path.

---

## 8. Permanent erasure / user retirement (the I2 right-to-erasure path)

Teardown is productized in **one canonical script**, `scripts/retire_user.py`,
so the destroy logic lives in exactly one place (symmetric to
`provision_user.py`). It is never invoked by hand for routine retirement —
two callers dispatch it through `.github/workflows/retire.yml`:

| Trigger | Reason | Path |
|---|---|---|
| 30 days after a canceled sub's period ends | `canceled-grace` | afair-web cron `grace-period-cleanup.mjs` → dispatch `retire.yml` |
| User clicks "Delete my account" (after export) | `user-requested` | afair-web `deleteAccount` server action → cancel Stripe → dispatch `retire.yml` |

`retire_user.py <clerk_user_id> --reason <r>` does, idempotently:

1. `fly apps destroy <app> --yes` — machine + volume + cert in one call.
2. Remove the vanity CNAME from the afair.ai Cloudflare zone.
3. Callback `POST /api/internal/retired` → afair-web sets `deleted_at`,
   `status='deleted'`, and **wipes the secrets escrow** (dead ciphertext
   once the volume is gone).

**Important:** a *canceled* subscription is NEVER paused — the user paid
for the period and keeps full use until it ends; only `period_end + 30d`
(grace) or an explicit user delete triggers teardown.

### Manual run (break-glass / abuse / test cleanup)

```bash
# Dry-run first — prints the plan, touches nothing.
RETIRE_CALLBACK_SECRET=... CLOUDFLARE_API_TOKEN=... \
  uv run python scripts/retire_user.py <clerk_user_id> --reason manual --dry-run

# Real run. App + DNS + control-plane row all handled.
uv run python scripts/retire_user.py <clerk_user_id> --reason manual

# Destroy the app but keep the CNAME (e.g. re-provisioning under same host):
uv run python scripts/retire_user.py <clerk_user_id> --keep-dns
```

Snapshots: `fly apps destroy` removes the volume; its snapshots age out on
Fly's retention (then gone). For compliance-grade *immediate* snapshot
erasure, also `fly volumes snapshots destroy <id>` before the retention
window — rarely needed, but documented.

### Go-live checklist for the retire flow

The teardown code ships dormant until these secrets are wired (all
additive — nothing breaks before they exist):

- [ ] `gh secret set RETIRE_CALLBACK_SECRET -R gowry/afair` (value in
      `.env.secrets.backup`) — retire.yml → callback.
- [ ] `fly secrets set RETIRE_CALLBACK_SECRET=... -a afair-web` — the
      `/api/internal/retired` route reads it at runtime.
- [ ] `gh secret set DATABASE_URL -R gowry/afair-web` — the grace cron
      selects candidates (was referencing a non-existent Actions secret
      before this refactor).
- [ ] `gh secret set GH_DISPATCH_TOKEN -R gowry/afair-web` — the grace
      cron dispatches retire.yml (same token the webhook uses at runtime
      on Fly; needed here as an Actions secret too).

Single-tenant makes erasure physically obvious: one user = one app = one
`fly apps destroy`.

---

## 9. Secrets management

| Secret | Live destination | Backup | Rotation |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Fly secret + `.env.local` | `.env.secrets.backup` | console.anthropic.com |
| `OPENAI_API_KEY` | Fly secret + `.env.local` | `.env.secrets.backup` | platform.openai.com |
| `FLY_API_TOKEN` (for CI) | GitHub repo secret | `.env.secrets.backup` | `fly tokens create deploy` |
| `AFAIR_AUTH_TOKEN` | Fly secret | `.env.secrets.backup` | regenerate via `python -c 'import secrets; print(secrets.token_urlsafe(32))'` |
| `AFAIR_SIGNUP_TOKEN` | Fly secret (afair) + GH secret (afair-web) | `.env.secrets.backup` | same |
| `AFAIR_JWT_SECRET` | Fly secret | `.env.secrets.backup` | `afair.mcp.oauth.jwt.generate_secret()` |
| `GITHUB_OAUTH_CLIENT_ID` / `SECRET` | Fly secret | `.env.secrets.backup` | github.com/settings/developers |
| `OAUTH_ISSUER` | Fly secret (not secret-secret, just config — required in prod) | `.env.secrets.backup` | n/a (URL doesn't rotate) |
| `RETIRE_CALLBACK_SECRET` | GH secret (afair, retire.yml) + Fly secret (afair-web, /api/internal/retired) | `.env.secrets.backup` (both repos) | regenerate via `token_urlsafe(32)`, update both destinations |

To rotate any of these:

1. Create new value at the provider
2. Update Fly: `fly secrets set ANTHROPIC_API_KEY=... -a afair`
3. Update `.env.local` (your laptop)
4. Update `.env.secrets.backup` (the annotated canonical record)
5. If it's a CI token, update GH secret too: `gh secret set FLY_API_TOKEN`
6. Revoke the old value at the provider

### Known gap: prod secrets not captured locally

`AFAIR_AUTH_TOKEN`, `AFAIR_JWT_SECRET`, and `AFAIR_SIGNUP_TOKEN` are
set in Fly (verified via `fly secrets list -a afair`) but their
plaintext values aren't in `.env.secrets.backup` yet — Fly only
stores digests after the initial set, so the values can't be read
back from the platform.

This violates the global secrets-backup convention. The fix is a
one-time coordinated rotation:

```bash
NEW=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
fly secrets set AFAIR_AUTH_TOKEN="$NEW" -a afair
echo "AFAIR_AUTH_TOKEN=$NEW" >> .env.secrets.backup  # plus annotations
```

`AFAIR_SIGNUP_TOKEN` rotation must be paired with
`gh secret set AFAIR_SIGNUP_TOKEN -R gowry/afair-web --body <new>`
so the landing-page form keeps working.

### Production boot requires these to be set

The `environment=fly` model validator refuses to start without:

- `AFAIR_AUTH_TOKEN` — otherwise the substrate would be world-writable
- `OAUTH_ISSUER` — otherwise JWTs are minted with the wrong `iss` claim
  and every OAuth handshake silently breaks (audit M1). For
  `mcp.afair.ai` set `OAUTH_ISSUER=https://mcp.afair.ai`.

Per global `CLAUDE.md`: a secret must always be in `.env.secrets.backup`
**before** it goes live anywhere else.

---

## 10. Smoke test the live server

```bash
# Health endpoint (HTTP — quick liveness check)
curl https://afair.fly.dev/health
# Expected: {"status":"ok"}

# MCP tool listing (proves the cross-vendor surface is alive)
curl -X POST https://mcp.afair.ai/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | jq

# Inspect the substrate live (read-only is safe)
fly ssh console -a afair -C "sqlite3 /data/vault/substrate.db 'SELECT COUNT(*) FROM events;'"
```

---

## 11. Rebuild the entity graph from substrate

The Phase 4 Track 1 entity graph (`entities`, `entity_mentions`,
`entity_edges`, `entity_merges`, `edge_invalidations`) is **regenerable**
per Invariant I3: every row is derived from substrate events +
extractor interpretations. If you want to throw it away and rebuild
(e.g., after a canonicalizer version bump), the recipe:

```bash
# 1. SSH to the running Fly machine
fly ssh console -a afair

# 2. Drop the entity-graph tables (substrate events untouched)
sqlite3 /data/vault/substrate.db <<'SQL'
DROP TABLE IF EXISTS edge_invalidations;
DROP TABLE IF EXISTS entity_merges;
DROP TABLE IF EXISTS entity_edges;
DROP TABLE IF EXISTS entity_mentions;
DROP TABLE IF EXISTS entities;
SQL

# 3. Restart the machine so the schema DDL re-runs and recreates the
#    empty tables with their I2 triggers
fly machine restart -a afair

# 4. Run the backfill — populates the empty graph from existing events
fly ssh console -a afair -C \
  "uv run python /app/scripts/backfill_entities.py"
```

Expected output: per-cycle progress lines, then a `backfill complete`
summary with the same shape as the worker's stats dict. The script
also writes an `observe` event recording the rebuild so the operation
is journaled in the substrate (I7).

**Idempotency:** safe to re-run. Skips events that already have
mentions. Skips invalidate events that already have a cascade marker.

**Bounded:** default cap of 100 cycles (`--max-cycles N` to override).
Each cycle is also LLM-budget-bounded so a runaway script can't burn
through your Anthropic quota.

**Off-server alternative:** download the substrate (`§4 Backup to
laptop`), run the backfill locally with `--vault-dir ./backup`, then
upload the result back. Useful when the running server is busy.

---

## 12. Common failures

### `address already in use` on local dev
You have a `afair` server already running. `lsof -i :8765` to find it.

### `database is locked` during deploy
The volume is still attached to a stopping machine. Wait 10s, retry.
(If you see it at runtime instead of at deploy, that's the audit
issue fixed in 749f5cb — `busy_timeout` now precedes `journal_mode`,
so concurrent `open_db` calls wait the lock out instead of raising.)

### `/health` returns 503
Run `fly logs -a afair`. Most common cause is the substrate DB being
unreachable at boot — usually fixed by `fly machine restart`.

### Deploy times out
`wait_timeout = "5m"` in `fly.toml`. If exceeded, the deploy is rolled back
to the previous image. Check `fly logs` for the actual error.

---

## 13. Logo & brand assets

Canonical brand files live in `assets/logo/`. Source of truth = the
original upload (`afair-elephant-original.jpg`); everything else is
derived via the recipe documented inline at the top of the directory.

| File | Use |
|---|---|
| `afair-elephant.png` (1024×1024, transparent) | Primary logo (light backgrounds) |
| `afair-elephant-inverse.png` (1024×1024, transparent, white silhouette) | Dark backgrounds / dark mode |
| `afair-elephant.svg` | Infinite-scale vector (potrace-traced) |
| `favicon-{16,32,48,180,192,256,512}.png` | Web favicon set |
| `favicon.ico` | Multi-size .ico for legacy browsers |
| `social-preview.png` (1280×640) | GitHub social preview, Open Graph image |

### Set the GitHub social preview (one-time manual)

`gh` CLI does not expose this. Manual UI step:

1. Open <https://github.com/gowry/afair/settings>
2. Under **Social preview**, click **Edit**
3. Upload `assets/logo/social-preview.png`
4. Save

Verify by sharing the repo link in Slack / Twitter / iMessage — preview
should show the lockup. Allow ~1 min for cache to refresh.

### Regenerate variants from a new original

If we ever swap the source elephant for a refined version, drop the new
file at `assets/logo/afair-elephant-original.jpg` (or `.png`) and re-run
the recipe; current generation chain uses ImageMagick 7 + potrace:

```bash
cd assets/logo
SRC=afair-elephant-original.jpg

# Master: tight-crop, threshold, square canvas at 1024
magick "$SRC" -fuzz 5% -trim +repage -threshold 50% \
  -gravity center -background white \
  -extent $(magick "$SRC" -fuzz 5% -trim +repage -format '%[fx:max(w,h)*1.25]' info:)x$(magick "$SRC" -fuzz 5% -trim +repage -format '%[fx:max(w,h)*1.25]' info:) \
  -resize 1024x1024 master-bw.png

# Primary PNG (transparent bg, black silhouette)
magick master-bw.png -transparent white afair-elephant.png

# Inverse for dark backgrounds
magick master-bw.png -negate -transparent black afair-elephant-inverse.png

# SVG via potrace
magick master-bw.png -alpha off -monochrome master-bw.pbm
potrace --svg --output afair-elephant.svg master-bw.pbm

# Favicons
for sz in 16 32 48 180 192 256 512; do
  magick afair-elephant.png -resize ${sz}x${sz} \
    -background none -gravity center -extent ${sz}x${sz} favicon-${sz}.png
done
magick favicon-16.png favicon-32.png favicon-48.png favicon.ico

# Social preview lockup (elephant left, "afair" wordmark right)
magick -size 1280x640 xc:white \
  \( afair-elephant.png -resize 360x360 \) -gravity west -geometry +160+0 -compose over -composite \
  -fill black -font Helvetica -pointsize 140 -gravity east -annotate +220+0 "afair" \
  social-preview.png

rm -f master-bw.png master-bw.pbm
```


## 14. Vault encryption (Stufe 1)

The substrate is encrypted at rest in two layers:

- **SQLite database** (`substrate.db`): SQLCipher whole-file AES-256
- **Object store** (`objects/<aa>/<rest>`): AES-256-GCM per blob, with
  a per-blob random nonce in a 4-byte magic prefix header

Both layers derive their working keys from a single master key
`AFAIR_VAULT_KEY` via HKDF with domain-separating info strings (see
`afair/substrate/encryption.py`). The boot validator refuses to start
the server when `ENVIRONMENT=fly` and the key is missing.

### 14.1 Generating + persisting the key

```bash
# Generate.
key=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')

# Set the Fly secret.
fly secrets set AFAIR_VAULT_KEY="$key" -a afair

# Persist to the backup file — same canonical-secret convention
# every other afair token follows. Edit by hand; never commit.
cat >> .env.secrets.backup <<EOF

# ─── VAULT ENCRYPTION (Stufe 1) ─────────────────
AFAIR_VAULT_KEY=$key
# placed: Fly secret on afair
# created: $(date -u +%F)Z, expires: no expiry (revocable by re-generating)
# rotation: NO ROTATION — the SQLite + blob ciphertexts under this key cannot
#   be re-keyed without a full vault re-write. To rotate: provision a fresh
#   vault under a new key, re-import via the MCP export surface.
EOF
```

### 14.2 First-time migration of an existing plaintext vault

The migration runs **inside the live machine** (where the production
volume is mounted), NOT via Fly's `release_command`. Release-command
machines are ephemeral and don't mount the app volume, so a
migration there silently finds an empty `/data/vault` and reports
"nothing to do" while the real DB stays plaintext.

Correct sequence (operator runs by hand, once per vault):

```bash
# 1. Generate + persist the master key (see 14.1).
# 2. Take a fresh Fly volume snapshot for rollback:
fly volumes snapshots create <volume-id> -a afair

# 3. Stage the secret BEFORE pushing the encryption-layer code:
fly secrets set --stage AFAIR_VAULT_KEY=<value> -a afair
git push   # triggers deploy

# 4. The new app boots, tries to open the still-plaintext file via
#    SQLCipher, fails with "HMAC check failed" + /health → 503.
#    This is expected — the migration hasn't run yet.

# 5. SSH in and run the migration on the production volume:
fly ssh console -a afair -C \
  "/usr/bin/env python /app/scripts/encrypt_existing_vault.py --skip-backup"

# 6. Restart the machine so the running process re-opens the now-
#    encrypted DB:
fly machine restart <machine-id> -a afair

# 7. Verify:
curl -sS https://mcp.afair.ai/health   # expect {"status":"ok"}
```

`--skip-backup` is appropriate because step 2 already took a Fly
snapshot — duplicating the vault inside the same 1 GB volume just
spikes disk usage without adding a recovery path the snapshot
doesn't already cover.

The script is **idempotent**: subsequent runs on an already-encrypted
vault detect that state via the SQLite-magic-header probe and return
"nothing to do".

#### Why not release_command — the failure modes we hit

Earlier we wired
`release_command = "python scripts/encrypt_existing_vault.py --skip-backup"`
into `fly.toml`. Two failure modes appeared:

1. **Release-command machines run without the production volume.**
   The migration saw an empty `/data/vault`, reported "nothing to
   do", and exited 0. The new image deployed. Then the live machine,
   with `AFAIR_VAULT_KEY` set and the still-plaintext substrate.db,
   failed to open the DB via SQLCipher with "HMAC check failed".
2. **Stdlib-sqlite-on-the-source side bug.** The original migration
   script opened the plaintext source via `import sqlite3` (stdlib).
   Stdlib has no `sqlcipher_export` function, and its
   `ATTACH ... KEY` clause is silently ignored. The script crashed
   with "file is not a database" on the ATTACH step. The fixed
   version uses `sqlcipher3` for BOTH source and target — sqlcipher3
   bundles upstream-compatible SQLite, so opening a plaintext file
   without setting PRAGMA key works as normal SQLite, then
   `sqlcipher_export` to the ATTACHed keyed target is what does the
   in-place encryption.

Both fixed in `scripts/encrypt_existing_vault.py`. The release_command
stanza was removed because issue #1 is structural — no script change
makes an ephemeral release machine see the production volume.

### 14.3 What happens if AFAIR_VAULT_KEY is lost

The data is gone. There is no recovery path. SQLCipher's key derivation
is deliberately one-way and AES-GCM is authenticated; without the
master key, the ciphertext is indistinguishable from random.

This is why the key MUST live in TWO places at all times:

- Fly's secret store (used by the running process)
- `.env.secrets.backup` on the operator's local machine (used by the
  operator + as the recovery anchor)

A daily off-machine sync of `.env.secrets.backup` to a separate
encrypted store (1Password vault, hardware backup) is the
recommended discipline.

### 14.4 Threat model — what this protects against

| Threat                                  | Stufe 1 status |
| --------------------------------------- | ---------------- |
| Disk theft / volume snapshot exfil      | Protected        |
| Cold backup leaked from Fly             | Protected        |
| Casual read of `.db` file or blob bytes | Protected        |
| Operator with `fly ssh` access          | NOT protected    |
| Fly insider access to running process   | NOT protected    |
| Side-channel inference via embeddings   | NOT protected    |

For the "NOT protected" rows, see the Stufe 2/3/4 designs in the
encryption roadmap (Stufe 2: per-event payload encryption with
operator-audit-logged KEK retrieval; Stufe 3: BYOK with
customer-managed keys; Stufe 4: Confidential Computing / TEE).

### 14.5 Operator-access transparency

Operators (the team running afair) have shell access to each user's
Fly machine. Every shell session writes a structured `observe` event
into THAT user's vault before any other action, so the user can see
in their own recall history that an admin connected, when, and why.

See `docs/runbooks/operator-vault-touch.md` (TODO) for the explicit
ritual. Until that runbook lands, the convention is: at the start of
every ad-hoc `fly ssh` session into a user machine, the operator runs:

```bash
python -m afair.admin record-operator-touch --reason "<one-line why>"
```

This writes a `remember` event with type_hint=`operator_action` so
the touch is auditable from the user's side.
