#!/usr/bin/env python3
"""Audit historical intake gaps; optionally repair one bounded batch.

Audit is the default. --apply extracts at most --limit unattempted events;
--repair-vectors additionally repairs at most --limit missing vectors from
existing successful interpretations. No raw event is rewritten or deleted.
"""

from __future__ import annotations

import argparse
import json

from afair.admin import _install_vault_key
from afair.agents.extraction_recovery import (
    extraction_audit,
    recover_missing_extractions,
    repair_missing_vectors,
)
from afair.mcp.context import ServerContext, clear_context, set_context
from afair.settings import load_settings
from afair.substrate import open_db
from afair.substrate.db import open_db_readonly


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="repair a bounded batch")
    parser.add_argument("--limit", type=int, default=5, help="events per repair stage (1-100)")
    parser.add_argument("--repair-vectors", action="store_true", help="also repair missing vectors")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100")
    if args.repair_vectors and not args.apply:
        parser.error("--repair-vectors requires --apply")
    settings = load_settings()
    _install_vault_key(settings)
    db = (
        open_db(settings.vault_dir, embedding_dim=settings.embedding_dim)
        if args.apply
        else open_db_readonly(settings.vault_dir)
    )
    try:
        result: dict[str, object] = {"before": extraction_audit(db)}
        if args.apply:
            set_context(
                ServerContext(
                    db=db,
                    background_usage_settings=settings,
                    vault_dir=settings.vault_dir,
                    inline_text_max_bytes=settings.inline_text_max_bytes,
                    extractor_model=settings.extractor_model,
                    vision_model=settings.vision_model,
                    transcription_model=settings.transcription_model,
                    embedding_model=settings.embedding_model,
                    embedding_dim=settings.embedding_dim,
                    semantic_recall_enabled=settings.semantic_recall_enabled,
                    anthropic_api_key=settings.anthropic_api_key,
                    openai_api_key=settings.openai_api_key,
                    gemini_api_key=settings.gemini_api_key,
                    voyage_api_key=settings.voyage_api_key,
                )
            )
            result["extractions"] = recover_missing_extractions(db, limit=args.limit)
            if args.repair_vectors:
                result["vectors"] = repair_missing_vectors(db, settings, limit=args.limit)
            result["after"] = extraction_audit(db)
        print(json.dumps(result, indent=2))
    finally:
        clear_context()
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
