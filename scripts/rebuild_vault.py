#!/usr/bin/env python3
"""Replay a vault's source events into a fresh, correctly-derived vault.

Why this exists: the derived layer (interpretations, the entity graph,
articles, embeddings, FTS) is a projection over the append-only substrate
(Invariant I3). When the derivation logic is fixed — e.g. the extractor used
to infer relations from mere co-occurrence and the graph filled with false
connections — the projection has to be rebuilt from the unchanged source. The
substrate is append-only at the DB level (events AND the entity tables carry
no-delete triggers, Invariant I2), so you cannot delete the bad rows in place.
The clean, invariant-respecting move is to REPLAY: copy the irreplaceable
source events (your remember/observe, plus the invalidations you issued) into
a fresh vault verbatim — same content hashes, same timestamps — then run the
fixed cold path over them to regenerate everything downstream.

What counts as a source event (kept):
  - every ``remember`` and ``observe`` — your actual input, the only
    irreplaceable data;
  - every ``invalidate`` whose target is one of those — a supersession you
    issued (remember(invalidates=...)), which encodes real intent.

What is regenerated, not copied:
  - ``entity_article`` / ``consolidation`` / ``entity_dedup_decision`` events,
    and the agent-issued invalidations that superseded prior articles — all
    machine derivations the fixed pipeline produces fresh.

Safety: the source vault is opened read-only in spirit (only SELECTs) and is
never mutated. Output goes to a SEPARATE destination directory, so the
original is untouched and the swap is a deliberate, reversible step you take
after verifying the result.

Usage:
    # Inspect what would be copied — no LLM, safe anywhere:
    uv run python scripts/rebuild_vault.py --source ~/vault --dest /tmp/v2 --dry-run

    # Full replay (needs ANTHROPIC_API_KEY / provider key + AFAIR_VAULT_KEY):
    uv run python scripts/rebuild_vault.py --source ~/vault --dest ~/vault.rebuilt

Then verify the destination, and only then swap it in for the source.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from afair.agents import extractor
from afair.agents.consolidator import Consolidator
from afair.agents.entity_articles import EntityArticleWorker
from afair.agents.entity_canonicalizer import EntityCanonicalizer
from afair.mcp.context import ServerContext, clear_context, set_context
from afair.settings import Settings
from afair.substrate import open_db, write_event
from afair.substrate.events import iter_events

if TYPE_CHECKING:
    import sqlite3

    from afair.substrate.events import Event

# Source events: the user's own input + their own supersessions. Everything
# else in the log is a cold-path derivation that the replay regenerates.
SOURCE_KINDS = frozenset({"remember", "observe"})
INVALIDATE_KIND = "invalidate"


def select_source_events(events: list[Event]) -> list[Event]:
    """The irreplaceable subset to copy verbatim, in original order.

    remember + observe, plus invalidate events that target one of those (a
    supersession the user issued). Invalidations targeting a derived event
    (an article the agent superseded) are dropped — the replay produces its
    own article supersessions.
    """
    kept: list[Event] = []
    source_hashes: set[str] = set()
    for ev in events:
        if ev.kind in SOURCE_KINDS:
            kept.append(ev)
            source_hashes.add(ev.content_hash)
    for ev in events:
        if ev.kind != INVALIDATE_KIND:
            continue
        targets = ev.parent_hashes or []
        if any(t in source_hashes for t in targets):
            kept.append(ev)
    # Preserve original chronology so created_at ordering and invalidation
    # targets line up the way they did the first time.
    kept.sort(key=lambda e: e.created_at)
    return kept


def copy_source_events(dest: sqlite3.Connection, sources: list[Event]) -> int:
    """Write each source event into the fresh vault verbatim.

    Same origin/kind/payload/parent_hashes → same content hash; created_at is
    carried over so the replayed log is chronologically identical to the
    original for the parts that matter.
    """
    copied = 0
    for ev in sources:
        write_event(
            dest,
            origin=ev.origin,
            kind=ev.kind,
            payload=ev.payload,
            parent_hashes=ev.parent_hashes or None,
            created_at=ev.created_at,
        )
        copied += 1
    return copied


def _run_workers_to_convergence(
    dest: sqlite3.Connection, settings: Settings, max_cycles: int
) -> dict[str, int]:
    """Drive canonicalizer → articles → consolidator until they stop changing.

    Each worker is idempotent and bounded per cycle (the same cap the live
    cold path uses), so a fixed number of cycles drains the backlog a vault of
    this size accumulates. Counts are accumulated for the run summary.
    """
    workers = [EntityCanonicalizer(), EntityArticleWorker(), Consolidator()]
    totals: dict[str, int] = {}
    for cycle in range(max_cycles):
        did_work = False
        for worker in workers:
            stats = worker.run(dest, settings)
            for k, v in stats.items():
                if isinstance(v, int):
                    totals[f"{worker.name}.{k}"] = totals.get(f"{worker.name}.{k}", 0) + v
                    if v > 0 and k not in {"llm_calls", "edges_skipped"}:
                        did_work = True
        if not did_work:
            totals["_cycles_run"] = cycle + 1
            break
    else:
        totals["_cycles_run"] = max_cycles
    return totals


def rebuild(
    source_dir: Path,
    dest_dir: Path,
    *,
    settings: Settings,
    max_cycles: int,
    dry_run: bool,
) -> dict[str, Any]:
    """Replay source_dir → dest_dir with the current (fixed) pipeline."""
    src_db = open_db(source_dir, embedding_dim=settings.embedding_dim)
    try:
        all_events = list(iter_events(src_db))
    finally:
        src_db.close()
    sources = select_source_events(all_events)

    summary: dict[str, Any] = {
        "total_events_in_source": len(all_events),
        "source_events_selected": len(sources),
        "by_kind_selected": _count_by_kind(sources),
        "dropped_derived": len(all_events) - len(sources),
    }
    if dry_run:
        summary["dry_run"] = True
        return summary

    if dest_dir.exists() and any(dest_dir.iterdir()):
        msg = f"destination {dest_dir} is not empty; refusing to overwrite. Pick a fresh dir."
        raise SystemExit(msg)
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_db = open_db(dest_dir, embedding_dim=settings.embedding_dim)
    # Mirror the live server's context so the replay uses the same models,
    # keys, and embedding config — otherwise re-extraction would silently run
    # on defaults and skip embeddings.
    ctx = ServerContext(
        db=dest_db,
        vault_dir=dest_dir,
        inline_text_max_bytes=settings.inline_text_max_bytes,
        extractor_model=settings.extractor_model,
        vision_model=settings.vision_model,
        transcription_model=settings.transcription_model,
        anthropic_api_key=settings.anthropic_api_key,
        openai_api_key=settings.openai_api_key,
        gemini_api_key=settings.gemini_api_key,
        voyage_api_key=settings.voyage_api_key,
        embedding_model=settings.embedding_model,
        embedding_dim=settings.embedding_dim,
        semantic_recall_enabled=settings.semantic_recall_enabled,
        cold_path_enabled=settings.cold_path_enabled,
        surprise_context_window=settings.surprise_context_window,
    )
    set_context(ctx)
    try:
        summary["copied"] = copy_source_events(dest_db, sources)
        # Re-extract every copied remember/observe with the fixed extractor.
        extracted = 0
        for ev in iter_events(dest_db):
            if ev.kind in SOURCE_KINDS:
                extractor.extract_sync(ev.id)
                extracted += 1
        summary["re_extracted"] = extracted
        summary["workers"] = _run_workers_to_convergence(dest_db, settings, max_cycles)
    finally:
        dest_db.close()
        clear_context()
    return summary


def _count_by_kind(events: list[Event]) -> dict[str, int]:
    out: dict[str, int] = {}
    for ev in events:
        out[ev.kind] = out.get(ev.kind, 0) + 1
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Existing vault to replay from.")
    parser.add_argument("--dest", type=Path, required=True, help="Fresh (empty) destination vault.")
    parser.add_argument("--max-cycles", type=int, default=200, help="Cold-path cycle cap.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the source-event selection and exit — no LLM, no writes.",
    )
    args = parser.parse_args(argv)

    settings = Settings()
    start = time.monotonic()
    summary = rebuild(
        args.source,
        args.dest,
        settings=settings,
        max_cycles=args.max_cycles,
        dry_run=args.dry_run,
    )
    summary["duration_seconds"] = round(time.monotonic() - start, 1)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
