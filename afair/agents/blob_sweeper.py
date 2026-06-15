"""OrphanBlobSweeper — object-store hygiene worker (cold path).

Quarantines blobs that no live event references. Orphans arise only from the
write-ordering gap between a blob hitting the object store and its event row
landing (see :mod:`afair.substrate.blob_gc`); the append-only substrate never
deletes an event to create one. Daily cadence — orphans accumulate slowly, if
at all — and the sweep quarantines rather than deletes, so a leak becomes
*visible* (counted, moved aside) without ever destroying the user's bytes
(I4). Emptying the quarantine is a separate, deliberate act.

What this worker MUST NEVER touch:
  - The events table — substrate is immutable (I2).
  - Any blob a live event references — that's the whole point of the mark set.
  - A blob younger than the grace window — it may be a mid-handshake upload
    whose remember(blob-ref) hasn't arrived yet.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from ..substrate.blob_gc import DEFAULT_ORPHAN_GRACE_SECONDS, sweep_orphan_blobs
from .cold_path import ColdPathWorker

if TYPE_CHECKING:
    import sqlite3

    from ..settings import Settings

log = structlog.get_logger(__name__)


class OrphanBlobSweeper(ColdPathWorker):
    """Quarantine unreferenced object-store blobs. No LLM; filesystem + SQL."""

    name = "orphan_blob_sweeper"
    interval_seconds = 24 * 3600  # daily — orphans are rare and slow-growing
    grace_seconds = DEFAULT_ORPHAN_GRACE_SECONDS

    def run(self, conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
        stats = sweep_orphan_blobs(
            Path(settings.vault_dir),
            conn,
            now=time.time(),
            grace_seconds=self.grace_seconds,
            quarantine=True,
        )
        if stats["quarantined"] or stats["orphaned"]:
            # Visibility over silence: a non-zero sweep is worth a line so an
            # operator can notice a leak trend rather than discover it on a
            # full volume.
            log.warning("cold_path.orphan_blobs_swept", **stats)
        return stats
