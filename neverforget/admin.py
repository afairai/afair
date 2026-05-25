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
  switch-embedding-model NEW_MODEL NEW_DIM
                 — atomically swap embedding models. Drops the existing
                    events_vec table, recreates it at the new dimension,
                    re-embeds every event with the new model, re-runs the
                    Bind agent. Interpretations + substrate untouched
                    (I2/I3). After this completes, update EMBEDDING_MODEL
                    and EMBEDDING_DIM env vars to match so the running
                    server uses the same provider on subsequent calls.
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


def switch_embedding_model(
    *, new_model: str, new_dim: int, dry_run: bool = False
) -> dict[str, object]:
    """Atomically migrate the vector store to a new embedding model.

    Steps:
      1. Drop the existing events_vec table (no-op if absent).
      2. Recreate it at the new dimension.
      3. For every event, embed with NEW_MODEL and insert the vector.
      4. Re-run the Bind agent on every event so the link graph reflects
         the new embedding space.

    Substrate rows are NEVER touched (I2). Interpretations are NEVER
    rewritten — they live in the Extractor's space, independent of the
    similarity model.

    After this completes, update EMBEDDING_MODEL/EMBEDDING_DIM in your
    env (or Fly secrets) so the running server uses the same model on
    subsequent calls. Forgetting to do that means new events embed with
    the old model and break the index.
    """
    settings = load_settings()
    db = open_db(settings.vault_dir, embedding_dim=settings.embedding_dim)

    # Pick the right API key for the NEW model — switching providers
    # often means switching keys too. We piggy-back on the embedding
    # settings dispatch.
    new_api_key: str | None = None
    if new_model.startswith("openai/") and settings.openai_api_key is not None:
        new_api_key = settings.openai_api_key.get_secret_value()
    elif new_model.startswith("voyage/") and settings.voyage_api_key is not None:
        new_api_key = settings.voyage_api_key.get_secret_value()
    elif new_model.startswith("gemini/") and settings.gemini_api_key is not None:
        new_api_key = settings.gemini_api_key.get_secret_value()
    elif new_model.startswith("anthropic/") and settings.anthropic_api_key is not None:
        new_api_key = settings.anthropic_api_key.get_secret_value()
    # fastembed/* needs no API key (local ONNX).

    stats: dict[str, object] = {
        "from_model": settings.embedding_model,
        "from_dim": settings.embedding_dim,
        "to_model": new_model,
        "to_dim": new_dim,
        "events_total": 0,
        "events_embedded": 0,
        "events_failed": 0,
        "binds_added": 0,
        "binds_skipped_no_neighbors": 0,
        "dry_run": dry_run,
    }

    log.info(
        "switch_embedding.start",
        from_model=settings.embedding_model,
        to_model=new_model,
        from_dim=settings.embedding_dim,
        to_dim=new_dim,
        dry_run=dry_run,
    )

    if dry_run:
        # Count what we'd do, but don't touch anything.
        for _ in iter_events(db, order="asc"):
            stats["events_total"] += 1  # type: ignore[operator]
        log.info("switch_embedding.done", **stats)
        return stats

    # Drop + recreate the vec table at the new dim. Interpretations
    # (including binder:v0 rows) stay — we'll overwrite the binder rows
    # below by re-running the Bind agent, which uses #retry-suffix logic
    # to break the UNIQUE constraint when a row exists.
    with db:
        db.execute("DROP TABLE IF EXISTS events_vec")
        db.execute(
            "CREATE VIRTUAL TABLE events_vec USING "
            f"vec0(content_hash TEXT PRIMARY KEY, embedding FLOAT[{new_dim}])"
        )

    # Pass 1: embed every event with the new model.
    for event in iter_events(db, order="asc"):
        stats["events_total"] += 1  # type: ignore[operator]
        interp = read_latest_interpretation(db, event.content_hash)
        extraction: dict[str, Any] = interp.extraction if interp is not None else {}
        text = _embedding_text_for_event(event, extraction)

        try:
            vec = embed_text(model=new_model, text=text, api_key=new_api_key)
        except EmbeddingError as e:
            log.warning("switch_embedding.embed_failed", event_id=event.id, error=str(e))
            stats["events_failed"] += 1  # type: ignore[operator]
            continue
        if len(vec) != new_dim:
            log.warning(
                "switch_embedding.dim_mismatch",
                event_id=event.id,
                got=len(vec),
                expected=new_dim,
            )
            stats["events_failed"] += 1  # type: ignore[operator]
            continue

        with db:
            db.execute(
                "INSERT INTO events_vec(content_hash, embedding) VALUES (?, ?)",
                (event.content_hash, serialize_vector(vec)),
            )
        stats["events_embedded"] += 1  # type: ignore[operator]

    # Pass 2: re-run Bind on every event so link graph reflects new space.
    for event in iter_events(db, order="asc"):
        row = db.execute(
            "SELECT embedding FROM events_vec WHERE content_hash = ?",
            (event.content_hash,),
        ).fetchone()
        if row is None:
            continue
        vec = list(struct.unpack(f"<{new_dim}f", row["embedding"]))
        result = find_and_record_links(db, event=event, embedding=vec)
        if result is None:
            stats["binds_skipped_no_neighbors"] += 1  # type: ignore[operator]
        else:
            stats["binds_added"] += 1  # type: ignore[operator]

    log.info("switch_embedding.done", **stats)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neverforget.admin")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("backfill", help="embed + bind missing events")
    sub.add_parser("backfill-dry", help="report what backfill would change")
    sub.add_parser("reprocess", help="re-extract events with failed interpretations")
    sub.add_parser("reprocess-dry", help="report what reprocess would change")

    switch = sub.add_parser(
        "switch-embedding-model",
        help="atomically swap embedding models (drops vec table, re-embeds, re-binds)",
    )
    switch.add_argument("model", help="new model string, e.g. fastembed/BAAI/bge-small-en-v1.5")
    switch.add_argument("dim", type=int, help="new dimension, e.g. 384")
    switch.add_argument(
        "--dry-run", action="store_true", help="report what would change, no writes"
    )

    args = parser.parse_args(argv)

    if args.command in {"backfill", "backfill-dry"}:
        result: dict[str, object] = backfill_vectors_and_links(  # type: ignore[assignment]
            dry_run=args.command.endswith("-dry")
        )
    elif args.command in {"reprocess", "reprocess-dry"}:
        result = reprocess_failed_extractions(  # type: ignore[assignment]
            dry_run=args.command.endswith("-dry")
        )
    elif args.command == "switch-embedding-model":
        result = switch_embedding_model(
            new_model=args.model, new_dim=args.dim, dry_run=args.dry_run
        )
    else:
        parser.print_help()
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
