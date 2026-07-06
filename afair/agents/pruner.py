"""Pruner — interpretation-layer hygiene worker (Phase 3).

Scope (v0):
  - OAuth code/login-state GC: deletes rows past their expires_at.
    Previously done in the WAL-checkpoint loop; consolidated here so
    the cold-path framework owns all interpretation-layer maintenance.
  - Stale-extractor-failure pruning: when a successful extractor run
    for an event exists at the same schema version (under any #retryN
    suffix), the older ``status: failed`` rows are kept only as audit.
    After N days we drop them — the success row remains as the live
    interpretation, and the diagnostic value of the failure has long
    since faded.
  - Decided edge_review queue hygiene (P1-1): decided edge_review rows
    in ``proposed_corrections`` older than the retention window are
    deleted. Safe because a decided edge's durable never-re-review guard
    is the append-only ``edge_reviews`` substrate table, not the queue
    row (proposed_corrections is non-substrate, no I2 triggers).

What Pruner MUST NEVER touch:
  - The events table — substrate is immutable per I2.
  - The events_vec table — embeddings are content-addressed; deleting
    one would break recall for that hash with no easy way to restore.
  - Successful interpretation rows — they ARE the live interpretation;
    deletion would force re-extraction (expensive) on next access.
  - Open (``status='proposed'``) proposals, and decided NON-edge_review
    proposals (retype / merge / merge_review) — the latter are
    entity_audit's anti-re-nag memory (its detectors re-scan the whole
    graph every cycle and would re-file the identical closed question if
    the decided row were gone).

Defaults are conservative; the worker is meant to be run-and-forget.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from .cold_path import ColdPathWorker

if TYPE_CHECKING:
    import sqlite3

    from ..settings import Settings

log = structlog.get_logger(__name__)


FAILED_EXTRACTION_RETENTION_DAYS = 30
"""Failed extraction rows older than this get deleted IF a successful
row for the same (event_hash, version, base producer) exists. The
failure stays around for a month for ops debug, then ages out."""

DECIDED_EDGE_REVIEW_RETENTION_DAYS = 30
"""Decided (status != 'proposed') edge_review queue rows older than this are
deleted. Safe: the durable never-re-review guard for a decided edge is the
append-only edge_reviews substrate table (edge_scorer's NOT EXISTS), not the
queue row. Decided retype/merge/merge_review rows are deliberately KEPT —
they are entity_audit's anti-re-nag memory (its detectors re-scan the whole
graph every cycle and would otherwise re-file the identical closed question).
proposed_corrections is non-substrate (no I2 triggers) so this deletion is
I2-honest — same footing as the OAuth-row GC above."""


class Pruner(ColdPathWorker):
    """OAuth + stale-failure pruning. No LLM; pure SQL."""

    name = "pruner"
    interval_seconds = 6 * 3600  # every 6h

    def run(self, conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "oauth_codes_deleted": 0,
            "oauth_login_state_deleted": 0,
            "stale_failed_extractions_deleted": 0,
            "decided_edge_reviews_deleted": 0,
        }
        now_iso = datetime.now(UTC).isoformat()

        with conn:
            cursor = conn.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (now_iso,))
            stats["oauth_codes_deleted"] = cursor.rowcount or 0
            cursor = conn.execute("DELETE FROM oauth_login_state WHERE expires_at < ?", (now_iso,))
            stats["oauth_login_state_deleted"] = cursor.rowcount or 0

        cutoff = (datetime.now(UTC) - timedelta(days=FAILED_EXTRACTION_RETENTION_DAYS)).isoformat()
        stats["stale_failed_extractions_deleted"] = _prune_stale_failed_extractions(conn, cutoff)

        # Decided edge_review queue hygiene (P1-1). Only edge_review rows, only
        # decided ones, only past the retention window — never open proposals,
        # never decided non-edge_review proposals (anti-re-nag memory).
        er_cutoff = (
            datetime.now(UTC) - timedelta(days=DECIDED_EDGE_REVIEW_RETENTION_DAYS)
        ).isoformat()
        with conn:
            cursor = conn.execute(
                "DELETE FROM proposed_corrections "
                "WHERE kind = 'edge_review' AND status != 'proposed' AND decided_at < ?",
                (er_cutoff,),
            )
        stats["decided_edge_reviews_deleted"] = cursor.rowcount or 0

        # Settings is unused for now; param retained for the Worker contract.
        _ = settings
        # time module imported but only used implicitly — keep the import
        # visible to readers since the worker conceptually "uses time".
        _ = time
        return stats


def _prune_stale_failed_extractions(conn: sqlite3.Connection, cutoff_iso: str) -> int:
    """Delete ``status: failed`` extractor rows older than the cutoff,
    but only when a successful row exists for the same (event_hash,
    version, base produced_by).

    The base producer comparison is approximate — we compare the
    portion before ``#retry`` so 'extractor:anthropic/claude#retry2'
    is treated as the same producer family as 'extractor:anthropic/claude'.
    """
    candidates = conn.execute(
        """
        SELECT id, event_hash, version, produced_by
        FROM interpretations
        WHERE produced_by LIKE 'extractor:%'
          AND produced_at < ?
          AND json_extract(extraction, '$.status') = 'failed'
        """,
        (cutoff_iso,),
    ).fetchall()

    deleted = 0
    for row in candidates:
        base_producer = (row["produced_by"] or "").split("#retry", 1)[0]
        # Is there ANY successful row at the same (event_hash, version)
        # produced by this family?
        success = conn.execute(
            """
            SELECT 1 FROM interpretations
            WHERE event_hash = ?
              AND version = ?
              AND produced_by LIKE ?
              AND json_extract(extraction, '$.status') = 'success'
            LIMIT 1
            """,
            (row["event_hash"], row["version"], base_producer + "%"),
        ).fetchone()
        if success is None:
            continue  # keep — no success exists yet, the failure is still informative
        with conn:
            conn.execute("DELETE FROM interpretations WHERE id = ?", (row["id"],))
        deleted += 1
        log.info(
            "pruner.dropped_stale_failure",
            event_hash=row["event_hash"][:24] + "...",
            produced_by=row["produced_by"],
        )
    return deleted
