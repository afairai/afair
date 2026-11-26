"""Targeted index repair from durable success, with no extraction or generation.

Only inline text and observe-event payloads are admitted. Other modalities need
source-specific verification and stay visible. Audit reads only; apply shares the
extractor lock/lease and emits completion only after checking exact FTS content.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from ..substrate import pipeline_events as pe
from ..substrate.events import read_event_by_id
from .extraction_jobs import MAX_JOB_FAILURES, ExtractionDeferred, extraction_lease
from .extractor import (
    _EXTRACTION_LOCKS,
    _api_key_for,
    _embedding_text_for_event,
    _enrich_fts_after_extraction,
    _extraction_searchable_text,
)
from .usage import BackgroundBudgetExceeded, background_usage

if TYPE_CHECKING:
    import sqlite3

    from ..settings import Settings


def _inspect(
    conn: sqlite3.Connection, event_id: str, *, own_lease: bool = False
) -> tuple[str, Any, dict[str, Any], str]:
    event = read_event_by_id(conn, event_id)
    if event is None:
        return "missing", None, {}, ""
    if conn.execute(
        "SELECT 1 FROM pipeline_events WHERE event_id=? AND stage IN (?,?) LIMIT 1",
        (event_id, pe.STAGE_EXTRACTION_COMPLETED, pe.STAGE_EXTRACTION_FAILED),
    ).fetchone():
        return "terminal", event, {}, ""
    row = conn.execute(
        """SELECT extraction FROM interpretations WHERE event_id=? AND event_hash=?
        AND produced_by LIKE 'extractor:%' ORDER BY produced_at DESC,version DESC,id DESC LIMIT 1""",
        (event_id, event.content_hash),
    ).fetchone()
    try:
        extraction = json.loads(row["extraction"]) if row else {}
    except (ValueError, TypeError):
        return "unsupported", event, {}, ""
    if not isinstance(extraction, dict):
        return "unsupported", event, {}, ""
    if (
        ("status" in extraction and extraction["status"] != "success")
        or not isinstance(extraction.get("summary"), str)
        or not isinstance(extraction.get("best_guess_kind"), str)
        or any(
            extraction.get(k, False) != 0
            for k in ("pdf_no_text_layer", "degraded", "fallback", "fallback_used")
        )
        or event.payload.get("content_type") not in ("text", "event")
        or not extraction.get("summary", "").strip()
    ):
        return "unsupported", event, extraction, ""
    job = conn.execute("SELECT * FROM extraction_jobs WHERE event_id=?", (event_id,)).fetchone()
    prior_repair = bool(
        job
        and job["purpose"] == "index_repair"
        and not job["terminal"]
        and job["failures"] < MAX_JOB_FAILURES
    )
    if job and (
        ((job["failures"] or job["last_error_type"]) and not prior_repair)
        or job["terminal"]
        or job["available_at"] > time.time()
        or (not own_lease and job["lease_until"] >= time.time())
    ):
        return "blocked", event, extraction, ""
    # Inline text/event normal extraction has no independently extracted body.
    expected = _extraction_searchable_text(event.payload, extraction, None)
    return "eligible", event, extraction, expected


def audit_indexes(conn: sqlite3.Connection, event_ids: list[str]) -> dict[str, int]:
    """Counts only, bounded explicit targets; never invokes a provider or writes."""
    _validate(event_ids)
    counts = dict.fromkeys(
        (
            "targets",
            "missing",
            "terminal",
            "unsupported",
            "blocked",
            "eligible",
            "fts_missing_or_different",
            "vector_missing",
        ),
        0,
    )
    counts["targets"] = len(event_ids)
    for event_id in event_ids:
        state, event, _, expected = _inspect(conn, event_id)
        counts[state] += 1
        if state != "eligible":
            continue
        rows = conn.execute(
            "SELECT searchable_text FROM events_fts WHERE content_hash=?", (event.content_hash,)
        ).fetchall()
        counts["fts_missing_or_different"] += int(len(rows) != 1 or rows[0][0] != expected)
        counts["vector_missing"] += int(
            conn.execute(
                "SELECT 1 FROM events_vec WHERE content_hash=?", (event.content_hash,)
            ).fetchone()
            is None
        )
    return counts


def _validate(event_ids: list[str]) -> None:
    if not 1 <= len(event_ids) <= 100 or len(set(event_ids)) != len(event_ids):
        raise ValueError("supply between 1 and 100 unique event IDs")


def repair_indexes(
    conn: sqlite3.Connection, settings: Settings, event_ids: list[str], *, embeddings: bool = False
) -> dict[str, int]:
    """Apply only explicitly selected events; metered embeddings are separately opt-in."""
    from .embedding import EmbeddingError, embed_text, serialize_vector

    _validate(event_ids)
    stats = {"targets": len(event_ids), "verified": 0, "skipped": 0, "failed": 0, "deferred": 0}
    for event_id in event_ids:
        key = (str(settings.vault_dir), event_id)
        try:
            with _EXTRACTION_LOCKS[hash(key) % len(_EXTRACTION_LOCKS)]:
                state, _, _, _ = _inspect(conn, event_id)
                if state != "eligible":
                    stats["skipped"] += 1
                    continue
                with extraction_lease(conn, event_id, purpose="index_repair"):
                    # Re-read exact event + latest matching result after cross-process claim.
                    # Our own active lease is expected; all other guards were checked above.
                    state, event, extraction, expected = _inspect(conn, event_id, own_lease=True)
                    if state != "eligible":
                        raise ExtractionDeferred("repair precondition changed")
                    _enrich_fts_after_extraction(
                        conn,
                        content_hash=event.content_hash,
                        payload=event.payload,
                        extraction=extraction,
                        extracted_text=None,
                    )
                    rows = conn.execute(
                        "SELECT searchable_text FROM events_fts WHERE content_hash=?",
                        (event.content_hash,),
                    ).fetchall()
                    if len(rows) != 1 or rows[0][0] != expected:
                        raise RuntimeError("FTS postcondition failed")
                    if (
                        embeddings
                        and conn.execute(
                            "SELECT 1 FROM events_vec WHERE content_hash=?", (event.content_hash,)
                        ).fetchone()
                        is None
                    ):
                        with background_usage(settings, "index_repair"):
                            vector = embed_text(
                                model=settings.embedding_model,
                                text=_embedding_text_for_event(event, extraction),
                                api_key=_api_key_for(settings.embedding_model, settings),
                            )
                        if len(vector) != settings.embedding_dim:
                            raise EmbeddingError("embedding dimension mismatch")
                        with conn:
                            if (
                                conn.execute(
                                    "SELECT 1 FROM events_vec WHERE content_hash=?",
                                    (event.content_hash,),
                                ).fetchone()
                                is None
                            ):
                                conn.execute(
                                    "INSERT INTO events_vec(content_hash,embedding) VALUES (?,?)",
                                    (event.content_hash, serialize_vector(vector)),
                                )
                    pe.record(
                        conn,
                        event_id=event.id,
                        event_hash=event.content_hash,
                        stage=pe.STAGE_EXTRACTION_COMPLETED,
                        producer="index_repair:v1",
                        detail="Verified stored extraction and exact FTS postcondition",
                    )
                    if not conn.execute(
                        "SELECT 1 FROM pipeline_events WHERE event_id=? AND event_hash=? "
                        "AND stage=? AND producer='index_repair:v1'",
                        (event.id, event.content_hash, pe.STAGE_EXTRACTION_COMPLETED),
                    ).fetchone():
                        raise RuntimeError("completion trace not durable")
                    stats["verified"] += 1
        except (BackgroundBudgetExceeded, ExtractionDeferred):
            stats["deferred"] += 1
        except Exception:
            # No exception message, identifier, payload or provider response in output.
            stats["failed"] += 1
    return stats
