# Operations — neverforget Phase 0

> **Status:** Living document. Update whenever a recipe changes in reality.
> **Audience:** the user and any future AI agent or contributor.

## 0. Current state

- **App:** `neverforget` on Fly (personal org)
- **Region:** `fra` (EU residency by default)
- **Volume:** `vault`, 1 GB, 5-day snapshot retention
- **Strategy:** `immediate` — single-machine deploys with brief downtime
- **URL:** `https://neverforget.fly.dev` (HTTPS auto-provisioned)
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

Watch progress: `gh run watch` or [github.com/gowry/neverforget/actions](https://github.com/gowry/neverforget/actions)

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
| `fly apps destroy neverforget` | ❌ Everything gone — app, machine, volume, snapshots. |

---

## 4. Backup to laptop (the I4 escape hatch)

Per Invariant I4, the user owns the substrate. They must always be able
to extract it.

```bash
# Pull the whole vault to your laptop
mkdir -p ~/neverforget-backups/$(date +%Y-%m-%d)
cd ~/neverforget-backups/$(date +%Y-%m-%d)

fly ssh sftp shell -a neverforget <<'EOF'
get /data/vault/substrate.db ./substrate.db
get -r /data/vault/objects ./objects
EOF

# Verify with sqlite3 directly — this is the canonical inspection
sqlite3 ./substrate.db "SELECT COUNT(*) FROM events;"
```

The downloaded directory is a complete, self-contained vault. You can
point a local `neverforget` server at it (`VAULT_DIR=./` in `.env.local`)
and it works without Fly.

---

## 5. Reattach an orphaned volume to a new machine

If a machine got destroyed and the volume is now unattached:

```bash
fly volumes list -a neverforget
# Note the volume id (vol_...).

fly machine list -a neverforget
# If there's no machine, create one and attach the existing volume.

fly machine create \
  --app neverforget \
  --region fra \
  --vm-size shared-cpu-1x \
  --vm-memory 512 \
  --volume vol_xxxxx:/data \
  --image registry.fly.io/neverforget:latest
```

For routine use, just `fly deploy` again — Fly will spin up a machine
that reattaches the existing volume if config matches.

---

## 6. Restore from a snapshot

Fly takes automatic daily snapshots (5-day retention).

```bash
fly volumes snapshots list -a neverforget
# Lists snapshots with ids, dates, sizes.

# Create a NEW volume from a snapshot (does NOT overwrite the existing one)
fly volumes create vault-restored \
  --app neverforget \
  --region fra \
  --size 1 \
  --snapshot-id vs_xxxxx

# Either point the app at the restored volume by editing fly.toml mounts,
# or manually copy data over from a temporary machine that has both mounted.
```

For a quick "I broke the substrate, give me yesterday's" path:

```bash
# 1. Stop the app to release the current volume
fly scale count 0 -a neverforget

# 2. List snapshots, pick one
fly volumes snapshots list -a neverforget

# 3. Destroy the current vault and create a new one from the snapshot
#    (DESTRUCTIVE — make sure you've backed up to laptop per §4 first!)
fly volumes destroy <current-vol-id> -a neverforget
fly volumes create vault --region fra --size 1 --snapshot-id <snapshot-id>

# 4. Scale back up
fly scale count 1 -a neverforget
```

---

## 7. Permanent erasure (the I2 right-to-erasure path)

When the user invokes their right to be forgotten and wants the data
**gone**:

```bash
# 1. Last-chance backup if they want a personal copy (see §4)

# 2. Stop the app
fly scale count 0 -a neverforget

# 3. Destroy the volume (this deletes all data, irreversible)
fly volumes list -a neverforget
fly volumes destroy <vol-id> -a neverforget

# 4. Destroy snapshots too (they retain data for 5 days otherwise)
fly volumes snapshots list -a neverforget
fly volumes snapshots destroy <snap-id> -a neverforget
# Repeat for each snapshot.

# 5. Destroy the app itself
fly apps destroy neverforget
```

Per Phase 9 work, the long-term answer for compliance-grade erasure is:
the orchestration layer can `fly apps destroy <user-app>` programmatically
on a right-to-erasure request. Single-tenant makes erasure physically
obvious.

---

## 8. Secrets management

| Secret | Live destination | Backup | Rotation |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Fly secret + `.env.local` | `.env.secrets.backup` | console.anthropic.com |
| `OPENAI_API_KEY` | Fly secret + `.env.local` | `.env.secrets.backup` | platform.openai.com |
| `FLY_API_TOKEN` (for CI) | GitHub repo secret | `.env.secrets.backup` | `fly tokens create deploy` |

To rotate any of these:

1. Create new value at the provider
2. Update Fly: `fly secrets set ANTHROPIC_API_KEY=... -a neverforget`
3. Update `.env.local` (your laptop)
4. Update `.env.secrets.backup` (the annotated canonical record)
5. If it's a CI token, update GH secret too: `gh secret set FLY_API_TOKEN`
6. Revoke the old value at the provider

Per global `CLAUDE.md`: a secret must always be in `.env.secrets.backup`
**before** it goes live anywhere else.

---

## 9. Smoke test the live server

```bash
# Health endpoint (HTTP — quick liveness check)
curl https://neverforget.fly.dev/health
# Expected: {"status":"ok"}

# MCP tool listing (proves the cross-vendor surface is alive)
curl -X POST https://neverforget.fly.dev/mcp/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | jq

# Inspect the substrate live (read-only is safe)
fly ssh console -a neverforget -C "sqlite3 /data/vault/substrate.db 'SELECT COUNT(*) FROM events;'"
```

---

## 10. Common failures

### `address already in use` on local dev
You have a `neverforget` server already running. `lsof -i :8765` to find it.

### `database is locked` during deploy
The volume is still attached to a stopping machine. Wait 10s, retry.

### `/health` returns 503
Run `fly logs -a neverforget`. Most common cause is the substrate DB being
unreachable at boot — usually fixed by `fly machine restart`.

### Deploy times out
`wait_timeout = "5m"` in `fly.toml`. If exceeded, the deploy is rolled back
to the previous image. Check `fly logs` for the actual error.
