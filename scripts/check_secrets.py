#!/usr/bin/env python3
"""Pre-deploy guard: verify a Fly app carries the secrets the server needs to boot.

The settings layer refuses to start when ``ENVIRONMENT=fly`` unless a few
secrets are present (see ``afair/settings.py`` validators). When an app is
missing one, the deploy itself succeeds but the machine crash-loops and the
only signal is an opaque 502/503 on ``/health``. That is exactly how the
afair-dev environment silently rotted three weeks behind prod.

This script turns that into a fast, named failure *before* the deploy:

    python scripts/check_secrets.py afair-solis-e03      # gate one app
    python scripts/check_secrets.py --diff afair-dev afair-solis-e03

It only reads secret *names* (``fly secrets list`` never exposes values), so it
is safe to run anywhere a deploy token can reach.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

# The secrets whose absence makes the server refuse to boot when
# ENVIRONMENT=fly. Mirrors the model_validator checks in afair/settings.py.
# Keep this in lockstep with those validators.
REQUIRED_FLY_SECRETS: frozenset[str] = frozenset(
    {
        "AFAIR_AUTH_TOKEN",  # _auth_required_in_prod
        "AFAIR_VAULT_KEY",  # _vault_key_required_in_prod
        "OAUTH_ISSUER",  # _oauth_issuer_required_in_prod
    }
)


def missing_required(
    present: set[str], required: frozenset[str] = REQUIRED_FLY_SECRETS
) -> set[str]:
    """Required secret names that are not present on the app. Empty set = good."""
    return set(required) - present


def name_diff(a: set[str], b: set[str]) -> tuple[set[str], set[str]]:
    """(only in a, only in b) — for an informational parity report."""
    return a - b, b - a


def fly_secret_names(app: str) -> set[str]:
    """The set of secret names configured on a Fly app (values never returned)."""
    out = subprocess.run(
        ["fly", "secrets", "list", "--json", "--app", app],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    rows = json.loads(out)
    # fly emits a list of {"name": ..., "digest": ..., "status": ...}.
    return {row["name"] for row in rows}


def cmd_check(app: str) -> int:
    present = fly_secret_names(app)
    missing = missing_required(present)
    if missing:
        print(
            f"✗ {app} is missing required secret(s): {', '.join(sorted(missing))}", file=sys.stderr
        )
        print(
            "  The server refuses to boot without these when ENVIRONMENT=fly.\n"
            "  Set them with: fly secrets set NAME=value --app " + app,
            file=sys.stderr,
        )
        return 1
    print(f"✓ {app} has all {len(REQUIRED_FLY_SECRETS)} required secrets.")
    return 0


def cmd_diff(app_a: str, app_b: str) -> int:
    a, b = fly_secret_names(app_a), fly_secret_names(app_b)
    only_a, only_b = name_diff(a, b)
    print(f"{app_a}: {len(a)} secrets | {app_b}: {len(b)} secrets")
    if only_a:
        print(f"  only on {app_a}: {', '.join(sorted(only_a))}")
    if only_b:
        print(f"  only on {app_b}: {', '.join(sorted(only_b))}")
    if not only_a and not only_b:
        print("  secret names match exactly.")
    # A name diff is informational (envs legitimately differ on optional
    # secrets), so this never fails the build on its own.
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", nargs="?", help="Fly app to gate for required secrets")
    parser.add_argument(
        "--diff",
        nargs=2,
        metavar=("APP_A", "APP_B"),
        help="report the secret-name differences between two apps",
    )
    args = parser.parse_args(argv)

    if args.diff:
        return cmd_diff(args.diff[0], args.diff[1])
    if args.app:
        return cmd_check(args.app)
    parser.error("give an app to check, or --diff APP_A APP_B")
    return 2  # unreachable; parser.error exits


if __name__ == "__main__":
    raise SystemExit(main())
