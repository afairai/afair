"""Admin / one-shot maintenance commands.

Runnable via ``python -m neverforget.admin <command>`` either locally
(against ~/.env.local) or remotely (``fly ssh console -a neverforget -C
"python -m neverforget.admin backfill"``).

Commands:
  backfill       — ensure every event has an embedding + bind record.
                    Idempotent: re-running is safe; existing rows are skipped.
  backfill-dry   — same flow but no writes; reports what would change.
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
from .agents.extractor import _embedding_text_for_event
from .agents.interpretation import read_latest_interpretation
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neverforget.admin")
    parser.add_argument(
        "command",
        choices=["backfill", "backfill-dry"],
        help="backfill = apply; backfill-dry = report what would change",
    )
    args = parser.parse_args(argv)
    dry = args.command == "backfill-dry"
    stats = backfill_vectors_and_links(dry_run=dry)
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
