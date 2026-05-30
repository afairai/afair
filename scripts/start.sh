#!/bin/sh
# Container entrypoint — wraps the Python app in Litestream replication
# when R2 credentials are configured, otherwise runs Python directly.
#
# Three behaviors based on environment:
#
#   1. R2 configured + no local DB     → restore from R2, then start replicating
#   2. R2 configured + local DB exists → start replicating from current state
#   3. R2 not configured               → run Python without replication (dev mode)
#
# Litestream's `replicate -exec` ties the subprocess lifecycle to the
# replication loop: a graceful exit triggers a final WAL flush before
# Litestream itself exits. SIGTERM (Fly shutdown) propagates correctly.

set -e

DB_PATH="${VAULT_DIR:-/data/vault}/substrate.db"
LITESTREAM_CONFIG="/app/litestream.yml"

if [ -n "$R2_ACCESS_KEY_ID" ] && [ -n "$R2_BUCKET" ] && [ -n "$R2_ACCOUNT_ID" ]; then
    if [ ! -f "$DB_PATH" ]; then
        echo "[start.sh] No local DB at $DB_PATH — attempting restore from R2..."
        # -if-replica-exists: noop if the replica is empty (fresh deploy).
        litestream restore -config "$LITESTREAM_CONFIG" -if-replica-exists "$DB_PATH" \
            || echo "[start.sh] No replica to restore from (this is normal on first deploy)."
    else
        echo "[start.sh] Local DB exists at $DB_PATH — continuing replication of current state."
    fi
    echo "[start.sh] Starting Litestream replication + Python app..."
    exec litestream replicate -config "$LITESTREAM_CONFIG" -exec "python -m afair"
else
    echo "[start.sh] R2 credentials not configured — running without replication."
    echo "[start.sh] Set R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ACCOUNT_ID, R2_BUCKET, R2_BUCKET_PATH to enable."
    exec python -m afair
fi
