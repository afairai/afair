"""Restart-safe repair of intake events lost before their first extraction.

The raw event is the durable queue: no transient executor state or retained
pipeline telemetry is required. Internal agent outputs are deliberately
excluded. Completed and failed attempts are left to their existing policies.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .cold_path import ColdPathWorker
from .foreground import background_should_yield

if TYPE_CHECKING:
    import sqlite3

    from ..settings import Settings

MAX_RECOVERIES_PER_RUN = 5
# Give the normal warm worker time to start. Slow/inflight attempts are also
# protected by the extractor's per-event lock and a second existence check.
RECOVERY_GRACE_SECONDS = 300

# Intake shapes, not an ontology of memory kinds. Agent-derived events carry
# produced_by; agent:<worker> origins are internal, never normal MCP intake.
_ELIGIBLE = """
    e.origin NOT LIKE 'agent:%'
    AND json_extract(e.payload, '$.produced_by') IS NULL
    AND (
        json_extract(e.payload, '$.content_type') IN ('text','text-large','binary','compound')
        OR (e.kind = 'observe' AND json_extract(e.payload, '$.content_type') = 'event')
    )
"""


def select_missing_extractions(
    conn: sqlite3.Connection,
    *,
    limit: int = MAX_RECOVERIES_PER_RUN,
    grace_seconds: int = RECOVERY_GRACE_SECONDS,
) -> list[tuple[str, str]]:
    """Oldest eligible events with no extractor attempt; no watermark can hide gaps."""
    cutoff = (datetime.now(UTC) - timedelta(seconds=grace_seconds)).isoformat()
    rows = conn.execute(
        f"""SELECT e.id, e.content_hash FROM events e
        WHERE {_ELIGIBLE} AND julianday(e.created_at) <= julianday(?)
          AND NOT EXISTS (SELECT 1 FROM interpretations i
              WHERE i.event_id = e.id AND i.produced_by LIKE 'extractor:%')
        ORDER BY e.created_at, e.id LIMIT ?""",
        (cutoff, limit),
    ).fetchall()
    return [(row["id"], row["content_hash"]) for row in rows]


MAX_DISCOVERY_ROWS = 500


def discover_missing_extractions(conn: sqlite3.Connection) -> int:
    """Queue at most 500 historical rows, advancing only after durable insertion.

    The lagged ULID horizon follows the repository's never-skip cursor contract.
    Recent eligible rows are queued with delayed availability, not discarded.
    """
    from ..substrate import watermarks

    cursor = watermarks.read_watermark_id(conn, "extraction_recovery_discovery") or ""
    rows = conn.execute(
        f"""WITH batch AS MATERIALIZED (
        SELECT * FROM events WHERE id > ? AND id < ? ORDER BY id LIMIT ?)
        SELECT e.id,e.created_at,({_ELIGIBLE}) AS eligible,
        EXISTS(SELECT 1 FROM interpretations i WHERE i.event_id=e.id
               AND i.produced_by LIKE 'extractor:%') AS attempted
        FROM batch e ORDER BY e.id""",
        (cursor, watermarks._lag_threshold_id(), MAX_DISCOVERY_ROWS),
    ).fetchall()
    if not rows:
        return 0
    # A crash between durable queue insertion and cursor advancement merely
    # repeats this batch; INSERT OR IGNORE and the attempt guard are idempotent.
    with conn:
        for row in rows:
            if row["eligible"] and not row["attempted"]:
                available = (
                    datetime.fromisoformat(row["created_at"]).timestamp() + RECOVERY_GRACE_SECONDS
                )
                conn.execute(
                    "INSERT OR IGNORE INTO extraction_jobs(event_id,available_at) VALUES (?,?)",
                    (row["id"], available),
                )
    watermarks.write_watermark(
        conn,
        "extraction_recovery_discovery",
        through_id=rows[-1]["id"],
        through_created_at=rows[-1]["created_at"],
    )
    return len(rows)


def extraction_audit(conn: sqlite3.Connection) -> dict[str, int]:
    """Read-only census independent of telemetry retention; contains no payloads."""
    row = conn.execute(
        f"""SELECT COUNT(*) AS eligible,
            COALESCE(SUM(NOT EXISTS(SELECT 1 FROM interpretations i
                WHERE i.event_id=e.id AND i.produced_by LIKE 'extractor:%')),0) AS missing,
            COALESCE(SUM(EXISTS(SELECT 1 FROM interpretations i
                WHERE i.event_id=e.id AND i.produced_by LIKE 'extractor:%'
                AND COALESCE(json_extract(i.extraction,'$.status'),'') != 'failed')),0) AS succeeded,
            COALESCE(SUM(NOT EXISTS(SELECT 1 FROM events_vec v
                WHERE v.content_hash=e.content_hash)),0) AS missing_vectors
            FROM events e WHERE {_ELIGIBLE}"""
    ).fetchone()
    return {
        "eligible": row["eligible"],
        "missing_extraction": row["missing"],
        "successful_extraction": row["succeeded"],
        "failed_only": row["eligible"] - row["missing"] - row["succeeded"],
        "missing_vectors": row["missing_vectors"],
    }


class ExtractionRecoveryWorker(ColdPathWorker):
    name = "extraction_recovery"
    interval_seconds = 300

    def run(self, conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
        return recover_missing_extractions(conn)


def recover_missing_extractions(
    conn: sqlite3.Connection,
    *,
    limit: int = MAX_RECOVERIES_PER_RUN,
) -> dict[str, int]:
    from .extraction_jobs import ExtractionDeferred
    from .extractor import extract_sync
    from .usage import BackgroundBudgetExceeded

    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    scanned = discover_missing_extractions(conn)
    candidates = conn.execute(
        """SELECT j.event_id FROM extraction_jobs j
        WHERE j.purpose='initial' AND j.available_at <= ? AND j.lease_until < ?
        ORDER BY j.available_at,j.event_id LIMIT ?""",
        (time.time(), time.time(), limit),
    ).fetchall()
    stats = {
        "scanned": scanned,
        "candidates": len(candidates),
        "attempted": 0,
        "deferred": 0,
        "errors": 0,
    }
    for row in candidates:
        event_id = row["event_id"]
        if background_should_yield():
            stats["deferred"] = len(candidates) - stats["attempted"]
            break
        # Discovery can race a warm completion. Retire that bounded stale
        # queue entry without a provider call, including deterministic failures.
        if conn.execute(
            "SELECT 1 FROM interpretations WHERE event_id=? "
            "AND produced_by LIKE 'extractor:%' LIMIT 1",
            (event_id,),
        ).fetchone():
            with conn:
                conn.execute(
                    "DELETE FROM extraction_jobs WHERE event_id=? AND purpose='initial' "
                    "AND lease_until < ?",
                    (event_id, time.time()),
                )
            continue
        try:
            extract_sync(event_id, only_if_missing=True)
        except (BackgroundBudgetExceeded, ExtractionDeferred):
            # Lease backoff keeps an unextractable oldest event from starving
            # later candidates; no failed interpretation burns a retry.
            stats["deferred"] += 1
            continue
        except Exception:
            # A failure before an interpretation lands stays discoverable.
            stats["errors"] += 1
        stats["attempted"] += 1
    return stats


def repair_missing_vectors(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    limit: int = MAX_RECOVERIES_PER_RUN,
) -> dict[str, int]:
    """Opt-in repair using successful extraction, never reprocess or rebuild an index."""
    from ..substrate.events import read_event_by_id
    from .embedding import EmbeddingError, embed_text, serialize_vector
    from .extractor import _EXTRACTION_LOCKS, _api_key_for, _embedding_text_for_event
    from .interpretation import read_latest_interpretation
    from .usage import BackgroundBudgetExceeded, background_usage

    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    rows = conn.execute(
        f"""SELECT e.id, e.content_hash FROM events e WHERE {_ELIGIBLE}
        AND EXISTS (SELECT 1 FROM interpretations i WHERE i.event_id=e.id
            AND i.produced_by LIKE 'extractor:%'
            AND COALESCE(json_extract(i.extraction,'$.status'),'') != 'failed')
        AND NOT EXISTS (SELECT 1 FROM events_vec v WHERE v.content_hash=e.content_hash)
        AND NOT EXISTS (SELECT 1 FROM extraction_jobs j WHERE j.event_id=e.id
            AND (j.available_at > ? OR j.lease_until >= ?))
        ORDER BY e.created_at, e.id LIMIT ?""",
        (time.time(), time.time(), limit),
    ).fetchall()
    stats = {"candidates": len(rows), "repaired": 0, "failed": 0, "deferred": 0}
    for index, row in enumerate(rows):
        if background_should_yield():
            stats["deferred"] = len(rows) - index
            break
        key = (str(settings.vault_dir), row["id"])
        from .extraction_jobs import ExtractionDeferred, extraction_lease

        try:
            with (
                _EXTRACTION_LOCKS[hash(key) % len(_EXTRACTION_LOCKS)],
                extraction_lease(conn, row["id"], purpose="vector"),
            ):
                if (
                    conn.execute(
                        "SELECT 1 FROM events_vec WHERE content_hash=?", (row["content_hash"],)
                    ).fetchone()
                    is not None
                ):
                    continue
                event = read_event_by_id(conn, row["id"])
                interp = read_latest_interpretation(conn, row["content_hash"])
                if event is None or interp is None or interp.extraction.get("status") == "failed":
                    continue
                with background_usage(settings, "vector_repair"):
                    vector = embed_text(
                        model=settings.embedding_model,
                        text=_embedding_text_for_event(event, interp.extraction),
                        api_key=_api_key_for(settings.embedding_model, settings),
                    )
                if len(vector) != settings.embedding_dim:
                    raise EmbeddingError("embedding dimension mismatch")
                with conn:
                    if (
                        conn.execute(
                            "SELECT 1 FROM events_vec WHERE content_hash=?", (row["content_hash"],)
                        ).fetchone()
                        is None
                    ):
                        conn.execute(
                            "INSERT INTO events_vec(content_hash,embedding) VALUES (?,?)",
                            (row["content_hash"], serialize_vector(vector)),
                        )
                        stats["repaired"] += 1
        except (BackgroundBudgetExceeded, ExtractionDeferred):
            stats["deferred"] += 1
        except EmbeddingError:
            stats["failed"] += 1
    return stats
