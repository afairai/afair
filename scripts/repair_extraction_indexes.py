#!/usr/bin/env python3
"""Audit explicit successful intake IDs; --apply verifies/rebuilds FTS only.

--embeddings additionally uses the normal metered embedding provider, never an
extractor or generation model. Output is aggregate-only. Default opens mode=ro.
"""

from __future__ import annotations

import argparse
import json

from afair.admin import _install_vault_key
from afair.agents.index_repair import audit_indexes, repair_indexes
from afair.settings import load_settings
from afair.substrate import open_db
from afair.substrate.db import open_db_readonly


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--event-id", action="append", required=True, help="explicit target; repeatable"
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--embeddings", action="store_true", help="opt-in metered missing embeddings"
    )
    args = parser.parse_args(argv)
    if not 1 <= len(args.event_id) <= 100 or len(set(args.event_id)) != len(args.event_id):
        parser.error("supply between 1 and 100 unique event IDs")
    if args.embeddings and not args.apply:
        parser.error("--embeddings requires --apply")
    settings = load_settings()
    _install_vault_key(settings)
    conn = (
        open_db(settings.vault_dir, embedding_dim=settings.embedding_dim)
        if args.apply
        else open_db_readonly(settings.vault_dir)
    )
    try:
        result = {"before": audit_indexes(conn, args.event_id)}
        if args.apply:
            result["repair"] = repair_indexes(
                conn, settings, args.event_id, embeddings=args.embeddings
            )
            result["after"] = audit_indexes(conn, args.event_id)
        print(json.dumps(result))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
