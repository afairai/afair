"""Background WAL checkpoint loop.

SQLite in WAL mode appends to ``substrate.db-wal`` until a checkpoint
folds those writes back into the main file. Without periodic
checkpoints the WAL file grows unbounded — readers stay fast, but
backups and restarts get slower. The auto-checkpoint heuristic (1000
pages, ~4MB) usually handles this, but on quiet/long-running servers
the WAL can sit large for hours.

This module runs a daemon thread that issues ``PRAGMA wal_checkpoint(PASSIVE)``
every N seconds. PASSIVE is non-blocking: it folds back what it can
without disturbing readers/writers. No big bang stalls. The thread
opens its own connection so it doesn't compete with handler traffic.
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import TYPE_CHECKING

import structlog

from .db import open_db

if TYPE_CHECKING:
    from pathlib import Path

log = structlog.get_logger(__name__)


def start_checkpoint_loop(
    vault_dir: Path, *, embedding_dim: int = 1536, interval_seconds: int = 300
) -> threading.Thread:
    """Spawn a daemon thread that issues ``wal_checkpoint(PASSIVE)`` periodically.

    Returns the thread for visibility / testing. The thread is a daemon so
    it dies with the process — no shutdown ceremony needed.

    The first checkpoint runs after ``interval_seconds``; we don't run one
    immediately at boot because the just-started server has no WAL volume
    to compact yet.
    """

    def loop() -> None:
        while True:
            time.sleep(interval_seconds)
            conn = None
            try:
                conn = open_db(vault_dir, embedding_dim=embedding_dim)
                row = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                # PRAGMA returns (busy, log, checkpointed) tuple. log = WAL
                # frame count BEFORE the checkpoint; checkpointed = how many
                # we managed to fold back. Useful as a health signal over time.
                if row is not None:
                    log.info(
                        "wal.checkpoint",
                        busy=row[0],
                        log_frames=row[1],
                        checkpointed=row[2],
                        interval_seconds=interval_seconds,
                    )
            except Exception as e:
                log.warning("wal.checkpoint_failed", error=str(e))
            finally:
                if conn is not None:
                    with contextlib.suppress(Exception):
                        conn.close()

    thread = threading.Thread(target=loop, name="wal-checkpoint", daemon=True)
    thread.start()
    log.info("wal.checkpoint_loop_started", interval_seconds=interval_seconds)
    return thread
