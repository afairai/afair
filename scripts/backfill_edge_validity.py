#!/usr/bin/env python3
"""One-shot ADR-0008 validity backfill for legacy entity edges.

The backfill is local, append-only, bounded, and idempotent.  It never calls an
LLM: each legacy edge uses an existing event-temporal interpretation when one
is available, otherwise its source event's recorded time becomes a
low-confidence reference-time span.  The normal temporal worker can refine
that span later.

Usage:
    uv run python scripts/backfill_edge_validity.py --dry-run
    uv run python scripts/backfill_edge_validity.py
    uv run python scripts/backfill_edge_validity.py --batch-size 250
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from afair.settings import Settings
from afair.substrate import backfill_missing_edge_validity, open_db, write_event
from afair.substrate.db import set_vault_key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill append-only entity-edge validity.")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--vault-dir", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--actor", choices=("Codex", "Claude Code"), default="Codex")
    parser.add_argument("--generator", default="Codex GPT-5.6 Sol")
    args = parser.parse_args(argv)
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")

    settings = Settings()
    if args.vault_dir is not None:
        settings = settings.model_copy(update={"vault_dir": args.vault_dir})
    db_path = settings.vault_dir / "substrate.db"
    if not db_path.exists():
        sys.stderr.write(f"no substrate.db found at {db_path}\n")
        return 2
    if settings.vault_key is not None:
        set_vault_key(settings.vault_key.get_secret_value().encode("utf-8"))

    conn = open_db(settings.vault_dir, embedding_dim=settings.embedding_dim)
    total_examined = 0
    total_written = 0
    try:
        missing = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM entity_edges e
                WHERE NOT EXISTS (
                    SELECT 1 FROM edge_validity_spans s WHERE s.edge_id = e.id
                )
                """
            ).fetchone()[0]
        )
        if args.dry_run:
            sys.stdout.write(f"legacy edges missing validity: {missing}\n")
            return 0

        remaining = missing
        while remaining:
            result = backfill_missing_edge_validity(conn, limit=args.batch_size)
            total_examined += result["examined"]
            total_written += result["written"]
            remaining = result["remaining"]
            sys.stdout.write(
                f"examined={total_examined} written={total_written} remaining={remaining}\n"
            )
            if result["examined"] == 0:
                break

        stamp = datetime.now(ZoneInfo("Europe/Vienna")).strftime("%Y-%m-%d %H:%M")
        signature = (
            f"— [Actor: {args.actor} · Generator: {args.generator} · "
            f"{stamp} Europe/Vienna · erstellt]"
        )
        write_event(
            conn,
            origin="agent",
            kind="observe",
            payload={
                "content_type": "event",
                "action": "backfill_edge_validity",
                "subject": "ADR-0008 legacy entity edges",
                "result": f"completed {signature}",
                "examined": total_examined,
                "written": total_written,
                "remaining": remaining,
                "signature": signature,
            },
        )
    finally:
        conn.close()
    return 0 if remaining == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
