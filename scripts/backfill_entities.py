#!/usr/bin/env python3
"""One-shot entity-graph backfill (Phase 4 Track 1 Stage 5).

Walks the existing substrate and runs the EntityCanonicalizer worker in
a loop until it has nothing left to canonicalize or the configured cycle
cap is hit. Each loop iteration uses the same budget as a normal cold-
path cycle, so the script naturally respects LLM rate limits.

Why a script and not "just let the cold-path worker catch up": the
worker fires every 120 seconds with a hard cap of 10 events per cycle.
A vault with N pre-existing events takes N/10 * 120s = ~20 minutes per
100 events. For the initial bootstrap (50+ events sitting in the vault
when Stage 1 lands), running this script gets the graph populated in
under a minute.

Idempotency: safe to re-run. Already-canonicalized events have
``entity_mentions`` rows already, so the worker's
``_find_uncanonicalized_events`` query skips them. Cascade marker rows
keep already-cascaded invalidate events out of the cycle too.

Self-modification audit (I7): the script writes one final ``observe()``
event recording its own activity so the backfill itself is in the
substrate.

Usage:
    uv run python scripts/backfill_entities.py
    uv run python scripts/backfill_entities.py --max-cycles 50
    uv run python scripts/backfill_entities.py --vault-dir /tmp/test-vault

Environment:
    VAULT_DIR  — overrides the default ~/vault
    All standard settings (ANTHROPIC_API_KEY, EMBEDDING_MODEL, etc.) are
    loaded from the same .env / .env.local as the running server.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

from neverforget.agents.entity_canonicalizer import EntityCanonicalizer
from neverforget.mcp.context import ServerContext, set_context
from neverforget.settings import Settings
from neverforget.substrate import open_db
from neverforget.substrate.events import write_event

DEFAULT_MAX_CYCLES = 100


def _did_work(stats: dict[str, Any]) -> bool:
    """A cycle did real work iff it actually wrote something — at least
    one mention (created/matched), one edge, or one cascade.

    NOTE: we deliberately do NOT count ``events_canonicalized`` alone,
    because the worker counts events even when they yield zero mentions
    (an extractor that returned malformed entities). Without the
    no-mentions marker in place this would loop forever; with the marker
    in place it's still a more honest heuristic ("did this cycle change
    the entity graph?") than the raw event counter.
    """
    return (
        stats.get("entities_created", 0) > 0
        or stats.get("entities_matched_exact", 0) > 0
        or stats.get("entities_matched_llm", 0) > 0
        or stats.get("invalidations_cascaded", 0) > 0
    )


def _accumulate(totals: dict[str, int], cycle: dict[str, int]) -> None:
    for k, v in cycle.items():
        if isinstance(v, int):
            totals[k] = totals.get(k, 0) + v


def _log_backfill_run(
    settings: Settings, totals: dict[str, int], cycles_run: int, duration_seconds: float
) -> None:
    """Write an observe() event recording what the backfill did.

    Lives in the substrate as a regular event so the I7 "self-modification
    is journaled" invariant covers backfill activity too — anyone reading
    the substrate later sees that the entity graph was materialized via
    this script with these stats.
    """
    db = open_db(settings.vault_dir, embedding_dim=settings.embedding_dim)
    try:
        payload: dict[str, Any] = {
            "content_type": "event",
            "action": "backfill_entities",
            "subject": "phase4_track1_stage5",
            "result": "completed",
            "cycles_run": cycles_run,
            "duration_seconds": round(duration_seconds, 2),
            **{k: int(v) for k, v in totals.items()},
        }
        write_event(db, origin="agent", kind="observe", payload=payload)
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One-shot entity-graph backfill for an existing neverforget vault."
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=DEFAULT_MAX_CYCLES,
        help=f"Hard cap on iterations to prevent runaway (default {DEFAULT_MAX_CYCLES})",
    )
    parser.add_argument(
        "--vault-dir",
        type=Path,
        default=None,
        help="Override vault directory (defaults to settings.vault_dir / $VAULT_DIR / ~/vault)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-cycle progress lines",
    )
    args = parser.parse_args(argv)

    settings = Settings()
    if args.vault_dir is not None:
        settings = settings.model_copy(update={"vault_dir": args.vault_dir})

    if not (settings.vault_dir / "substrate.db").exists():
        sys.stderr.write(
            f"no substrate.db found at {settings.vault_dir / 'substrate.db'}; nothing to backfill\n"
        )
        return 2

    # Establish a ServerContext so connect_for_thread inside the worker
    # picks up the right vault path + API keys.
    set_context(
        ServerContext(
            vault_dir=settings.vault_dir,
            inline_text_max_bytes=settings.inline_text_max_bytes,
            extractor_model=settings.extractor_model,
            anthropic_api_key=settings.anthropic_api_key,
            openai_api_key=settings.openai_api_key,
            gemini_api_key=settings.gemini_api_key,
            voyage_api_key=settings.voyage_api_key,
            embedding_model=settings.embedding_model,
            embedding_dim=settings.embedding_dim,
            semantic_recall_enabled=settings.semantic_recall_enabled,
            cold_path_enabled=False,
        )
    )

    worker = EntityCanonicalizer()
    db = open_db(settings.vault_dir, embedding_dim=settings.embedding_dim)
    totals: dict[str, int] = {}
    cycles_run = 0
    started = time.monotonic()
    try:
        for cycle in range(1, args.max_cycles + 1):
            stats = worker.run(db, settings)
            cycles_run = cycle
            _accumulate(totals, stats)
            if not args.quiet:
                sys.stdout.write(
                    f"cycle {cycle:3d}: "
                    f"events={stats.get('events_canonicalized', 0):2d} "
                    f"created={stats.get('entities_created', 0):2d} "
                    f"matched_exact={stats.get('entities_matched_exact', 0):2d} "
                    f"matched_llm={stats.get('entities_matched_llm', 0):2d} "
                    f"edges={stats.get('edges_created', 0):2d} "
                    f"cascade={stats.get('invalidations_cascaded', 0):2d} "
                    f"llm_calls={stats.get('llm_calls', 0):2d} "
                    f"llm_errors={stats.get('llm_errors', 0):2d}\n"
                )
                sys.stdout.flush()
            if not _did_work(stats):
                break
    finally:
        db.close()

    duration = time.monotonic() - started

    if not args.quiet:
        sys.stdout.write("\nbackfill complete.\n")
        for k in sorted(totals):
            sys.stdout.write(f"  {k}: {totals[k]}\n")
        sys.stdout.write(f"  cycles_run: {cycles_run}\n")
        sys.stdout.write(f"  duration_seconds: {duration:.2f}\n")

    _log_backfill_run(settings, totals, cycles_run=cycles_run, duration_seconds=duration)
    return 0


if __name__ == "__main__":
    sys.exit(main())
