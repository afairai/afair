#!/usr/bin/env python3
"""Supervised backlog drain for the entity deduplicator (ADR-0003 Phase 2).

The scheduled cold-path deduplicator judges at most a handful of same-name
clusters per 6-hour cycle — fine for steady state, far too slow to work
down a pre-existing backlog (the v1-era same-name splits). This operator
tool loops ``EntityDeduplicator.run()`` at a raised per-cycle cap so the
backlog drains in supervised batches (default 25 clusters per invocation),
with a checkup (scripts/checkup_entities.py) between batches.

Because it reuses the worker's ``run()``, the drain inherits every guard
for free: the operator-governed skip, keep-separate markers, Slice 3 kind
unification, the Slice 4 deliberate-split skip, the 0.75 merge floor, and
the conservative judge prompt. It never adds a blind same-name merge.

Reversibility (I7): every merge is undoable via ``write_merge_invalidation``
(or the merge_review reject path), after which the operator-governed guard
permanently protects that cluster from re-merge. The drain writes one final
``observe`` event recording the run — the same audit anchor
scripts/backfill_entities.py uses.

Usage:
    uv run python scripts/drain_entity_dedup.py --dry-run
    uv run python scripts/drain_entity_dedup.py --max-clusters 25 --sleep 2
    uv run python scripts/drain_entity_dedup.py --vault-dir /tmp/test-vault

Environment:
    VAULT_DIR  — overrides the default vault directory.
    Standard LLM/API-key settings are loaded from the same .env / .env.local
    as the running server (the judge uses settings.entity_dedup_model).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

from afair.agents import entity_dedup as ed
from afair.agents.entity_dedup import EntityDeduplicator
from afair.settings import Settings
from afair.substrate import open_db
from afair.substrate.db import set_vault_key
from afair.substrate.events import write_event

DEFAULT_MAX_CLUSTERS = 25
DEFAULT_SLEEP_SECONDS = 2.0


def _accumulate(totals: dict[str, int], cycle: dict[str, Any]) -> None:
    for k, v in cycle.items():
        if isinstance(v, int):
            totals[k] = totals.get(k, 0) + v


def _dry_run_report(db: Any) -> None:
    """Print the same-name cluster census + per-cluster detail. No LLM, no
    writes — reuses the worker's read-only candidate/member helpers."""
    keys = ed._candidate_keys(db)
    if not keys:
        sys.stdout.write("no same-name clusters — nothing to drain.\n")
        return
    same_kind = 0
    cross_kind = 0
    sys.stdout.write(f"{len(keys)} same-name cluster(s):\n\n")
    for key in keys:
        members = ed._load_members(db, key)
        kinds = {m.entity.kind for m in members}
        if len(kinds) > 1:
            cross_kind += 1
        else:
            same_kind += 1
        sys.stdout.write(f"  {key!r}: {len(members)} members, kinds={sorted(kinds)}\n")
        for m in members:
            sys.stdout.write(
                f"      - {m.entity.id}  kind={m.entity.kind}  mentions={m.mention_count}\n"
            )
    sys.stdout.write(f"\nsame-kind clusters: {same_kind}, cross-kind clusters: {cross_kind}\n")
    sys.stdout.write("dry-run: no LLM calls, no writes.\n")


def _log_drain_run(
    db: Any, totals: dict[str, int], *, cycles_run: int, duration_seconds: float
) -> None:
    """Write an observe() event recording the drain (I7 audit anchor)."""
    payload: dict[str, Any] = {
        "content_type": "event",
        "action": "drain_entity_dedup",
        "subject": "adr0003_phase2_backlog_drain",
        "result": "completed",
        "cycles_run": cycles_run,
        "duration_seconds": round(duration_seconds, 2),
        **{k: int(v) for k, v in totals.items()},
    }
    write_event(db, origin="agent", kind="observe", payload=payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Supervised backlog drain for the entity deduplicator (ADR-0003 Phase 2)."
    )
    parser.add_argument(
        "--max-clusters",
        type=int,
        default=DEFAULT_MAX_CLUSTERS,
        help=f"Total clusters to examine this invocation (default {DEFAULT_MAX_CLUSTERS}).",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=DEFAULT_SLEEP_SECONDS,
        help=f"Seconds to sleep between cycles for rate-limit headroom "
        f"(default {DEFAULT_SLEEP_SECONDS}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the cluster census + per-cluster detail; make NO LLM calls and NO writes.",
    )
    parser.add_argument(
        "--vault-dir",
        type=Path,
        default=None,
        help="Override vault directory (defaults to settings.vault_dir / $VAULT_DIR).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-cycle progress lines.",
    )
    args = parser.parse_args(argv)

    settings = Settings()
    if args.vault_dir is not None:
        settings = settings.model_copy(update={"vault_dir": args.vault_dir})

    if not (settings.vault_dir / "substrate.db").exists():
        sys.stderr.write(
            f"no substrate.db found at {settings.vault_dir / 'substrate.db'}; nothing to drain\n"
        )
        return 2

    # Install the vault key before opening, exactly as server boot does
    # (server.py). Without this an encrypted (production) vault opens as
    # SQLCipher ciphertext and fails with "file is not a database". Keyless
    # (local/test) vaults leave the module default untouched.
    if settings.vault_key is not None:
        set_vault_key(settings.vault_key.get_secret_value().encode("utf-8"))

    db = open_db(settings.vault_dir, embedding_dim=settings.embedding_dim)
    try:
        if args.dry_run:
            _dry_run_report(db)
            return 0

        # One worker constructed at the batch cap; loop run() until the cap
        # is reached or a cycle examines nothing (backlog drained). Each
        # run() re-skips clusters it already merged / marked keep-separate,
        # so successive cycles progress through the backlog.
        worker = EntityDeduplicator(max_clusters_per_cycle=args.max_clusters)
        totals: dict[str, int] = {}
        cycles_run = 0
        started = time.monotonic()
        while True:
            stats = worker.run(db, settings)
            cycles_run += 1
            _accumulate(totals, stats)
            if not args.quiet:
                sys.stdout.write(
                    f"cycle {cycles_run:3d}: "
                    f"examined={stats.get('clusters_examined', 0):3d} "
                    f"merged={stats.get('clusters_merged', 0):3d} "
                    f"entities_merged={stats.get('entities_merged', 0):3d} "
                    f"kinds_unified={stats.get('kinds_unified', 0):3d} "
                    f"kept_separate={stats.get('skipped_not_same', 0):3d} "
                    f"deliberate_split={stats.get('skipped_deliberate_split', 0):3d} "
                    f"errors={stats.get('llm_errors', 0):3d}\n"
                )
                sys.stdout.flush()
            examined = totals.get("clusters_examined", 0)
            if stats.get("clusters_examined", 0) == 0 or examined >= args.max_clusters:
                break
            time.sleep(args.sleep)

        duration = time.monotonic() - started
        if not args.quiet:
            sys.stdout.write("\ndrain complete.\n")
            for k in sorted(totals):
                sys.stdout.write(f"  {k}: {totals[k]}\n")
            sys.stdout.write(f"  cycles_run: {cycles_run}\n")
            sys.stdout.write(f"  duration_seconds: {duration:.2f}\n")

        _log_drain_run(db, totals, cycles_run=cycles_run, duration_seconds=duration)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
