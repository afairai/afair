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

## 7. Backup strategy + future RPO upgrade paths

**Current state:** Fly automatic daily volume snapshots with 14-day retention.
RPO is ~24h, RTO is minutes (snapshot restore + machine boot).

This is **acceptable for Phase 0** (founder dogfood + early invites). The
upgrade path is documented below; revisit when real users generate data
where 24h loss is meaningfully worse than 1h loss.

### Increase snapshot retention

Default after `fly volumes create … --snapshot-retention 5` is 5 days.
Bump to 14 with:

```bash
fly volumes list -a afair                # note volume id
fly volumes update <vol-id> -a afair --snapshot-retention 14
```

### List snapshots + restore

See §6.

### Upgrade paths when 24h RPO is too coarse

Three realistic options, in increasing order of complexity:

**A — Hourly snapshots via GitHub Actions cron** *(RPO ~1h)*

Add `.github/workflows/snapshot-hourly.yml`:

```yaml
name: Hourly substrate snapshot
on:
  schedule: [{cron: "0 * * * *"}]
jobs:
  snapshot:
    runs-on: ubuntu-latest
    steps:
      - uses: superfly/flyctl-actions/setup-flyctl@master
      - run: flyctl volumes snapshots create <vol-id> --app afair
        env:
          FLY_API_TOKEN: ${{ secrets.FLY_API_TOKEN_PROD }}
```

Zero new vendors. Just a scheduled job. Cost: extra storage for 24×
more snapshots, rotation via retention policy.

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
| Current (daily snapshots) | ~24h | Fly only | zero (Fly auto) | Volume snapshots on user's own host |
| A — Hourly snapshots cron | ~1h | Fly only | zero (GH Actions cron) | Same |
| B — LiteFS Cloud | <1s | Fly only | one API call per user namespace | User runs own LiteFS or volume snapshots |
| C — Litestream → external S3 | <1s | Fly + S3 vendor | bucket + token per user | User runs Litestream to their own S3 |

### Decision: stay at default until invites force the question

Phase 0 = me, one machine, one vault, daily-use validation. 24h RPO
costs me at most one day of memories if Fly's NVMe craters. Acceptable.

When the first paying user is provisioned (Phase 1+) revisit. **Most
likely path:** Option A immediately (just adds a cron), Option B if
~1h still hurts.

---

## 8. Permanent erasure (the I2 right-to-erasure path)

When the user invokes their right to be forgotten and wants the data
**gone**:

```bash
# 1. Last-chance backup if they want a personal copy (see §4)

# 2. Stop the app
fly scale count 0 -a afair

# 3. Destroy the volume (this deletes all data, irreversible)
fly volumes list -a afair
fly volumes destroy <vol-id> -a afair

# 4. Destroy snapshots too (they retain data for 5 days otherwise)
fly volumes snapshots list -a afair
fly volumes snapshots destroy <snap-id> -a afair
# Repeat for each snapshot.

# 5. Destroy the app itself
fly apps destroy afair
```

Per Phase 9 work, the long-term answer for compliance-grade erasure is:
the orchestration layer can `fly apps destroy <user-app>` programmatically
on a right-to-erasure request. Single-tenant makes erasure physically
obvious.

---

## 9. Secrets management

| Secret | Live destination | Backup | Rotation |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Fly secret + `.env.local` | `.env.secrets.backup` | console.anthropic.com |
| `OPENAI_API_KEY` | Fly secret + `.env.local` | `.env.secrets.backup` | platform.openai.com |
| `FLY_API_TOKEN` (for CI) | GitHub repo secret | `.env.secrets.backup` | `fly tokens create deploy` |

To rotate any of these:

1. Create new value at the provider
2. Update Fly: `fly secrets set ANTHROPIC_API_KEY=... -a afair`
3. Update `.env.local` (your laptop)
4. Update `.env.secrets.backup` (the annotated canonical record)
5. If it's a CI token, update GH secret too: `gh secret set FLY_API_TOKEN`
6. Revoke the old value at the provider

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
