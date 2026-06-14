#!/usr/bin/env python
"""Break-glass: recover a user's per-app secrets from the encrypted escrow.

A user's vault key, bearer, and jwt live only on their own Fly app. If that
app/volume is ever lost, the encrypted vault (and its snapshots, which are
SQLCipher-encrypted with that key) is unrecoverable WITHOUT these secrets.
provision_user.py escrows them — Fernet-encrypted with PLATFORM_ESCROW_KEY —
into afair-web's users.secrets_escrow. This tool decrypts that ciphertext
and re-sets the Fly secrets on the (recreated) app.

The afair-web DB holds only ciphertext; only PLATFORM_ESCROW_KEY (operator
hands + this tool) can read it. So losing the DB does not expose vault keys,
and losing the Fly app does not lose the vault.

Usage
=====
    # operator fetches the ciphertext, then:
    PLATFORM_ESCROW_KEY=... uv run python scripts/recover_user.py <clerk_user_id> --escrow '<ciphertext>'
    ... --dry-run    # decrypt + report which secrets, set nothing

    # fetch the ciphertext (needs DB access via fly proxy):
    #   fly proxy 15432:5432 -a afair-web-db &
    #   psql "<DATABASE_URL with host 127.0.0.1:15432>" -tA \
    #     -c "select secrets_escrow from users where clerk_user_id='<id>'"
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from provision_user import app_name_for, fly_set_secrets


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("clerk_user_id", help="Clerk userId whose vault to recover")
    ap.add_argument(
        "--escrow",
        default=None,
        help="The Fernet ciphertext from users.secrets_escrow. If omitted, read from stdin.",
    )
    ap.add_argument("--dry-run", action="store_true", help="Decrypt + report, set nothing")
    args = ap.parse_args()

    key = os.environ.get("PLATFORM_ESCROW_KEY")
    if not key:
        print("ERROR: PLATFORM_ESCROW_KEY must be set", file=sys.stderr)
        sys.exit(2)

    ciphertext = args.escrow or sys.stdin.read().strip()
    if not ciphertext:
        print("ERROR: no escrow ciphertext (pass --escrow or pipe it on stdin)", file=sys.stderr)
        sys.exit(2)

    from cryptography.fernet import Fernet, InvalidToken

    try:
        bundle = json.loads(Fernet(key.encode()).decrypt(ciphertext.encode()).decode())
    except InvalidToken:
        print(
            "ERROR: decryption failed — wrong PLATFORM_ESCROW_KEY for this escrow", file=sys.stderr
        )
        sys.exit(3)

    app = app_name_for(args.clerk_user_id)
    secrets_map = {
        "AFAIR_VAULT_KEY": bundle["vault_key"],
        "AFAIR_AUTH_TOKEN": bundle["auth_token"],
        "AFAIR_JWT_SECRET": bundle["jwt_secret"],
        "AFAIR_SIGNUP_TOKEN": bundle["signup_token"],
    }
    print(f"Recovered {len(secrets_map)} secrets for {args.clerk_user_id} -> app {app}")
    # fly_set_secrets prints only the KEYS, never the values.
    fly_set_secrets(app, secrets_map, dry=args.dry_run)
    if args.dry_run:
        print("(dry run — Fly secrets NOT set)")
    else:
        print(f"Re-set on {app}. Redeploy the app to apply: flyctl deploy -a {app} --image <ref>")


if __name__ == "__main__":
    main()
