#!/usr/bin/env python3
"""Read-only daily provider usage report, using the configured vault key.

Run: uv run python scripts/background_usage.py --day 2026-11-26
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from afair.agents.usage import usage_summary
from afair.settings import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--day", type=date.fromisoformat, help="UTC day (YYYY-MM-DD); defaults to today"
    )
    parser.add_argument("--vault-dir", type=Path, help="Override the configured vault directory")
    args = parser.parse_args(argv)
    settings = Settings()
    if args.vault_dir is not None:
        settings = settings.model_copy(update={"vault_dir": args.vault_dir})
    print(json.dumps(usage_summary(settings, args.day.isoformat() if args.day else None), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
