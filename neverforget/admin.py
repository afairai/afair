"""Admin / one-shot maintenance commands.

Runnable via ``python -m neverforget.admin <command>`` either locally
(against ~/.env.local) or remotely (``fly ssh console -a neverforget -C
"python -m neverforget.admin backfill"``).

Commands:
  backfill       — ensure every event has an embedding + bind record.
                    Idempotent: re-running is safe; existing rows are skipped.
  backfill-dry   — same flow but no writes; reports what would change.
  reprocess      — re-run the Extractor on events whose latest interpretation
                    is ``status: failed`` (or events with no interpretation
                    at all). Useful after deploying extractor fixes — old
                    failures get a second chance against the new pipeline.
  reprocess-dry  — same flow, no writes.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from typing import TYPE_CHECKING, Any

import structlog

from .agents.binder import find_and_record_links
from .agents.embedding import EmbeddingError, embed_text, serialize_vector
from .agents.extractor import _embedding_text_for_event, extract_sync
from .agents.interpretation import read_latest_interpretation
from .mcp.context import ServerContext, clear_context, set_context
from .settings import load_settings
from .substrate import iter_events, open_db

if TYPE_CHECKING:
    from .settings import Settings

log = structlog.get_logger(__name__)


def _api_key_for_embeddings(settings: Settings) -> str | None:
    model = settings.embedding_model
    if model.startswith("openai/") and settings.openai_api_key is not None:
        return settings.openai_api_key.get_secret_value()
    if model.startswith("anthropic/") and settings.anthropic_api_key is not None:
        return settings.anthropic_api_key.get_secret_value()
    if settings.openai_api_key is not None:
        return settings.openai_api_key.get_secret_value()
    return None


def backfill_vectors_and_links(*, dry_run: bool = False) -> dict[str, int]:
    """Ensure every event has an embedding + bind record. Idempotent.

    Phase 1: embed any event missing from events_vec.
    Phase 2: re-run the Bind agent on events missing a binder:v0
             interpretation row (now that all events have embeddings,
             nearest-neighbor queries are productive).
    """
    settings = load_settings()
    db = open_db(settings.vault_dir, embedding_dim=settings.embedding_dim)
    api_key = _api_key_for_embeddings(settings)

    stats: dict[str, int] = {
        "events_total": 0,
        "embeddings_already_present": 0,
        "embeddings_added": 0,
        "embeddings_failed": 0,
        "binds_already_present": 0,
        "binds_added": 0,
        "binds_skipped_no_neighbors": 0,
    }

    log.info("backfill.start", dry_run=dry_run, model=settings.embedding_model)

    # ── pass 1: embeddings ─────────────────────────────────────────────────
    for event in iter_events(db, order="asc"):
        stats["events_total"] += 1
        existing = db.execute(
            "SELECT 1 FROM events_vec WHERE content_hash = ?",
            (event.content_hash,),
        ).fetchone()
        if existing is not None:
            stats["embeddings_already_present"] += 1
            continue

        if dry_run:
            stats["embeddings_added"] += 1
            continue

        interp = read_latest_interpretation(db, event.content_hash)
        extraction: dict[str, Any] = interp.extraction if interp is not None else {}
        text = _embedding_text_for_event(event, extraction)

        try:
            vec = embed_text(model=settings.embedding_model, text=text, api_key=api_key)
        except EmbeddingError as e:
            log.warning("backfill.embedding_failed", event_id=event.id, error=str(e))
            stats["embeddings_failed"] += 1
            continue

        with db:
            db.execute(
                "INSERT INTO events_vec(content_hash, embedding) VALUES (?, ?)",
                (event.content_hash, serialize_vector(vec)),
            )
        stats["embeddings_added"] += 1
        log.info("backfill.embedding_added", event_id=event.id, dim=len(vec))

    # ── pass 2: bind records ───────────────────────────────────────────────
    for event in iter_events(db, order="asc"):
        existing_bind = db.execute(
            """
            SELECT 1 FROM interpretations
            WHERE event_hash = ? AND produced_by = 'binder:v0'
            """,
            (event.content_hash,),
        ).fetchone()
        if existing_bind is not None:
            stats["binds_already_present"] += 1
            continue

        row = db.execute(
            "SELECT embedding FROM events_vec WHERE content_hash = ?",
            (event.content_hash,),
        ).fetchone()
        if row is None:
            stats["binds_skipped_no_neighbors"] += 1
            continue

        if dry_run:
            stats["binds_added"] += 1
            continue

        vec = list(struct.unpack(f"<{settings.embedding_dim}f", row["embedding"]))
        result = find_and_record_links(db, event=event, embedding=vec)
        if result is None:
            stats["binds_skipped_no_neighbors"] += 1
        else:
            stats["binds_added"] += 1

    log.info("backfill.done", **stats)
    return stats


def reprocess_failed_extractions(*, dry_run: bool = False) -> dict[str, int]:
    """Re-run the Extractor on events whose latest interpretation failed
    (or are missing one entirely).

    Substrate is immutable (I2) so we can't overwrite the failed row;
    we write a NEW interpretation row with the same ``produced_by`` but
    a later ``produced_at``. ``read_latest_interpretation`` already orders
    by ``produced_at DESC`` and skips ``status: failed``, so a successful
    re-run automatically supersedes the old failure for recall purposes.

    The failed row stays in the table as audit trail — exactly what I3
    is designed to support.
    """
    settings = load_settings()
    db = open_db(settings.vault_dir, embedding_dim=settings.embedding_dim)

    # Reprocess needs to call extract_sync, which reads context. Set up a
    # minimal ServerContext so the threaded extractor finds its config.
    ctx = ServerContext(
        db=db,
        vault_dir=settings.vault_dir,
        inline_text_max_bytes=settings.inline_text_max_bytes,
        extractor_model=settings.extractor_model,
        embedding_model=settings.embedding_model,
        embedding_dim=settings.embedding_dim,
        semantic_recall_enabled=settings.semantic_recall_enabled,
        anthropic_api_key=settings.anthropic_api_key,
        openai_api_key=settings.openai_api_key,
        gemini_api_key=settings.gemini_api_key,
    )
    set_context(ctx)

    stats: dict[str, int] = {
        "events_total": 0,
        "events_already_succeeded": 0,
        "events_reprocessed": 0,
        "events_still_failing": 0,
        "events_with_no_prior_interp": 0,
    }

    log.info("reprocess.start", dry_run=dry_run, model=settings.extractor_model)

    try:
        for event in iter_events(db, order="asc"):
            stats["events_total"] += 1

            # Decide whether to reprocess this event.
            row = db.execute(
                """
                SELECT extraction FROM interpretations
                WHERE event_hash = ? AND produced_by LIKE 'extractor:%'
                ORDER BY produced_at DESC, version DESC
                LIMIT 1
                """,
                (event.content_hash,),
            ).fetchone()

            if row is None:
                stats["events_with_no_prior_interp"] += 1
                needs_reprocess = True
            else:
                extraction = json.loads(row["extraction"])
                if extraction.get("status") == "failed":
                    needs_reprocess = True
                else:
                    stats["events_already_succeeded"] += 1
                    needs_reprocess = False

            if not needs_reprocess:
                continue

            if dry_run:
                stats["events_reprocessed"] += 1
                continue

            try:
                extract_sync(event.id)
            except Exception as e:
                log.warning("reprocess.error", event_id=event.id, error=str(e))
                stats["events_still_failing"] += 1
                continue

            # Verify success: re-read the latest extractor row.
            row2 = db.execute(
                """
                SELECT extraction FROM interpretations
                WHERE event_hash = ? AND produced_by LIKE 'extractor:%'
                ORDER BY produced_at DESC, version DESC
                LIMIT 1
                """,
                (event.content_hash,),
            ).fetchone()
            if row2 is None:
                stats["events_still_failing"] += 1
                continue
            ext = json.loads(row2["extraction"])
            if ext.get("status") == "failed":
                stats["events_still_failing"] += 1
            else:
                stats["events_reprocessed"] += 1
                log.info("reprocess.success", event_id=event.id)
    finally:
        clear_context()

    log.info("reprocess.done", **stats)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neverforget.admin")
    parser.add_argument(
        "command",
        choices=["backfill", "backfill-dry", "reprocess", "reprocess-dry"],
        help=(
            "backfill        = embed + bind missing events. "
            "reprocess       = re-extract events with failed/missing interpretations. "
            "*-dry           = report what would change without writing."
        ),
    )
    args = parser.parse_args(argv)

    if args.command in {"backfill", "backfill-dry"}:
        stats = backfill_vectors_and_links(dry_run=args.command.endswith("-dry"))
    else:
        stats = reprocess_failed_extractions(dry_run=args.command.endswith("-dry"))

    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
