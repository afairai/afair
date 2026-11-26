#!/usr/bin/env python3
"""Read-only daily provider usage report, using the configured vault key.

Run: uv run python scripts/background_usage.py --day 2026-11-26
"""

from __future__ import annotations

import argparse
import json
import math
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
    parser.add_argument(
        "--infrastructure-usd",
        type=float,
        help="Daily infrastructure allocation from the operator invoice",
    )
    args = parser.parse_args(argv)
    if args.infrastructure_usd is not None and (
        not math.isfinite(args.infrastructure_usd) or args.infrastructure_usd < 0
    ):
        parser.error("infrastructure allocation must be a finite non-negative USD amount")
    settings = Settings()
    if args.vault_dir is not None:
        settings = settings.model_copy(update={"vault_dir": args.vault_dir})
    report = usage_summary(settings, args.day.isoformat() if args.day else None)
    operations = {}
    for row in report["workers"]:
        totals = operations.setdefault(row["operation"], {})
        for key in (
            "attempts",
            "settled_estimated_usd",
            "retained_reservations_usd",
            "unsettled_attempts",
            "unpriced_attempts",
        ):
            totals[key] = totals.get(key, 0) + row[key]
    report.update(
        operations=operations,
        infrastructure_usd=args.infrastructure_usd,
        cap_applies_to="completion",
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
