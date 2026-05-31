"""Provision a new per-user afair MCP instance on Fly.

CLAUDE.md §0.2 captured this script as "must exist before the first
paying invite." Per Invariant I8 each user gets their OWN Fly app
(separate volume, separate secrets, separate machine) — no shared
substrate, no risk that one user's data leaks into another's. This
script makes that provisioning a single command instead of a 20-step
fly-CLI checklist.

What it does (per invocation)
=============================
1. Mints fresh per-user secrets:
     - AFAIR_AUTH_TOKEN  (server bearer)
     - AFAIR_JWT_SECRET  (JWT signing key)
     - AFAIR_SIGNUP_TOKEN (scoped landing-page bearer, optional)
2. Creates a new Fly app named ``afair-u-<short>`` where <short> is
   a 6-char hash of the user's GitHub username (avoids leaking the
   username in DNS while staying deterministic).
3. Creates a 1 GB volume named ``vault`` in the configured region.
4. Sets all required Fly secrets on the new app (per-user values
   above plus the shared GitHub OAuth client credentials).
5. Deploys the SAME afair image to the new app (no rebuild).
6. Appends the per-user secrets to .env.secrets.backup with the
   canonical annotation block (per global CLAUDE.md secrets-backup
   convention — secrets must NEVER live only in Fly).
7. Prints a one-page summary: the user's MCP URL, the tokens, the
   next steps.

What it does NOT do
===================
- It does NOT add the user to the master billing account; that's a
  manual subscription/Stripe step kept out of the script for clarity.
- It does NOT configure DNS for a custom domain; the auto-generated
  ``<app>.fly.dev`` works for MCP clients out of the box.
- It does NOT enable hourly snapshots per-user yet (see
  docs/operations.md §7). Default Fly daily snapshots apply.

Usage
=====

    uv run python scripts/provision_user.py <github_username>
    uv run python scripts/provision_user.py <github_username> --region fra
    uv run python scripts/provision_user.py <github_username> --dry-run

Idempotency
===========
Re-running with the same github_username detects the existing app
and refuses to clobber it (dry-run prints the expected app name so
you can verify). To rotate a user's secrets, run a separate rotation
script (TODO — kept out of this commit to keep scope tight).
"""

from __future__ import annotations

import argparse
import hashlib
import secrets
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# ── configuration ──────────────────────────────────────────────────────────


DEFAULT_REGION = "fra"
DEFAULT_VM_SIZE = "shared-cpu-1x"
DEFAULT_VM_MEMORY_MB = 1024
DEFAULT_VOLUME_SIZE_GB = 1
DOCKER_IMAGE = "registry.fly.io/afair:latest"
"""Reuse the deployed prod image — provisioning a new user is metadata
only, no new build needed."""

ENV_SECRETS_BACKUP = Path(__file__).resolve().parent.parent / ".env.secrets.backup"


# ── per-user identity ──────────────────────────────────────────────────────


def app_name_for(github_username: str) -> str:
    """Deterministic per-user app name. Hash so the username doesn't
    leak in DNS lookups; 6 hex chars = 16M collision space which is
    fine for the multi-tenant scale (1k users gives ~ no collision
    probability)."""
    h = hashlib.sha256(github_username.lower().encode()).hexdigest()[:6]
    return f"afair-u-{h}"


# ── token generation ───────────────────────────────────────────────────────


def mint_token() -> str:
    """url-safe 256-bit secret. Same shape as the prod AFAIR_AUTH_TOKEN."""
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class UserSecrets:
    auth_token: str
    jwt_secret: str
    signup_token: str

    @classmethod
    def fresh(cls) -> UserSecrets:
        return cls(
            auth_token=mint_token(),
            jwt_secret=mint_token(),
            signup_token=mint_token(),
        )


# ── Fly CLI shims ──────────────────────────────────────────────────────────


def _run_fly(args: list[str], *, dry: bool, capture: bool = False) -> str:
    """Wrap flyctl invocations. Prints what it's about to run; in dry
    mode prints only. Returns stdout when capture=True."""
    cmd = ["flyctl", *args]
    print(f"  $ {' '.join(cmd)}")
    if dry:
        return ""
    result = subprocess.run(
        cmd,
        check=True,
        capture_output=capture,
        text=True,
    )
    return result.stdout if capture else ""


def fly_app_exists(app: str) -> bool:
    """Probe via ``flyctl apps list`` — exits 0 regardless, so we
    grep the output."""
    try:
        out = subprocess.run(
            ["flyctl", "apps", "list", "--json"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except subprocess.CalledProcessError:
        return False
    return f'"{app}"' in out


def fly_create_app(app: str, *, dry: bool) -> None:
    _run_fly(["apps", "create", app, "--name", app], dry=dry)


def fly_create_volume(app: str, *, region: str, size_gb: int, dry: bool) -> None:
    _run_fly(
        [
            "volumes",
            "create",
            "vault",
            "-a",
            app,
            "--region",
            region,
            "--size",
            str(size_gb),
            "--yes",  # skip confirmation
        ],
        dry=dry,
    )


def fly_set_secrets(app: str, secrets_map: dict[str, str], *, dry: bool) -> None:
    """Set all secrets in one ``fly secrets set`` call so the app
    restarts ONCE not N times. Values stay out of shell history
    because flyctl reads them from argv directly — same posture as
    the manual workflow we're automating."""
    args = ["secrets", "set", "-a", app, "--stage"]
    for key, value in secrets_map.items():
        args.append(f"{key}={value}")
    _run_fly(args, dry=dry)


def fly_deploy_image(app: str, *, image: str, dry: bool) -> None:
    _run_fly(["deploy", "-a", app, "--image", image, "--remote-only"], dry=dry)


# ── secrets backup write ──────────────────────────────────────────────────


def append_secrets_backup(*, app: str, github_username: str, s: UserSecrets) -> None:
    """Append the per-user secrets block to .env.secrets.backup.

    Format follows the global CLAUDE.md secrets-backup convention:
    each secret is annotated with where it's placed, when it was
    created, and how to rotate.
    """
    from datetime import UTC, datetime

    today = datetime.now(UTC).date().isoformat()
    block = f"""

# ───── PER-USER PROVISIONING — {app} ({github_username}) ─────
# Created by scripts/provision_user.py on {today}.
# These secrets gate ONLY the user's own app — never reuse across users.

AFAIR_AUTH_TOKEN_{app.upper().replace("-", "_")}={s.auth_token}
# placed: Fly secret on {app}
# created: {today}, expires: no expiry
# rotate: python3 -c 'import secrets; print(secrets.token_urlsafe(32))' \\
#         + flyctl secrets set AFAIR_AUTH_TOKEN=<new> -a {app}

AFAIR_JWT_SECRET_{app.upper().replace("-", "_")}={s.jwt_secret}
# placed: Fly secret on {app}
# created: {today}, expires: no expiry
# rotate: same as AFAIR_AUTH_TOKEN; existing JWTs invalidate immediately

AFAIR_SIGNUP_TOKEN_{app.upper().replace("-", "_")}={s.signup_token}
# placed: Fly secret on {app}
# created: {today}, expires: no expiry
# rotate: same as above; afair-web also needs the new value if it
#         posts signups to this user's app
"""
    with ENV_SECRETS_BACKUP.open("a", encoding="utf-8") as fh:
        fh.write(block)


# ── orchestration ─────────────────────────────────────────────────────────


def provision(
    *,
    github_username: str,
    region: str,
    dry: bool,
    enable_signup_token: bool,
) -> tuple[str, UserSecrets]:
    app = app_name_for(github_username)
    print(f"== Provisioning user '{github_username}' → app '{app}' ==")

    if not dry and fly_app_exists(app):
        print(f"ERROR: app '{app}' already exists — refusing to clobber", file=sys.stderr)
        sys.exit(2)

    if dry:
        print("  (dry run — no remote calls)")

    s = UserSecrets.fresh()
    secrets_map: dict[str, str] = {
        "AFAIR_AUTH_TOKEN": s.auth_token,
        "AFAIR_JWT_SECRET": s.jwt_secret,
        "OAUTH_ISSUER": f"https://{app}.fly.dev",
        "IDENTITY_ALLOWLIST": github_username.lower(),
        "ENVIRONMENT": "fly",
    }
    if enable_signup_token:
        secrets_map["AFAIR_SIGNUP_TOKEN"] = s.signup_token

    # The shared GitHub OAuth app credentials — same client_id/secret
    # for every user; the per-user IDENTITY_ALLOWLIST handles isolation.
    # Operator must export these in the shell before running this script.
    import os

    for shared_key in ("GITHUB_OAUTH_CLIENT_ID", "GITHUB_OAUTH_CLIENT_SECRET"):
        v = os.environ.get(shared_key)
        if not v:
            print(
                f"ERROR: env var {shared_key} must be set before running",
                file=sys.stderr,
            )
            sys.exit(2)
        secrets_map[shared_key] = v

    fly_create_app(app, dry=dry)
    fly_create_volume(app, region=region, size_gb=DEFAULT_VOLUME_SIZE_GB, dry=dry)
    fly_set_secrets(app, secrets_map, dry=dry)
    fly_deploy_image(app, image=DOCKER_IMAGE, dry=dry)

    if not dry:
        append_secrets_backup(app=app, github_username=github_username, s=s)

    return app, s


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("github_username", help="GitHub login of the user being provisioned")
    ap.add_argument("--region", default=DEFAULT_REGION)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned actions without executing",
    )
    ap.add_argument(
        "--no-signup-token",
        action="store_true",
        help="Skip the AFAIR_SIGNUP_TOKEN (only useful if the user "
        "isn't running their own signup landing page)",
    )
    args = ap.parse_args()

    app, s = provision(
        github_username=args.github_username,
        region=args.region,
        dry=args.dry_run,
        enable_signup_token=not args.no_signup_token,
    )

    print()
    print("=" * 60)
    print(f"  Provisioned: https://{app}.fly.dev")
    print("=" * 60)
    print(f"  MCP endpoint:   https://{app}.fly.dev/mcp")
    print(f"  Health:         https://{app}.fly.dev/health")
    print(f"  OAuth metadata: https://{app}.fly.dev/.well-known/oauth-authorization-server")
    print()
    print("  Tokens (also persisted in .env.secrets.backup):")
    print(f"    AFAIR_AUTH_TOKEN   = {s.auth_token}")
    print(f"    AFAIR_JWT_SECRET   = {s.jwt_secret}")
    print(f"    AFAIR_SIGNUP_TOKEN = {s.signup_token}")
    print()
    print("  Next steps:")
    print("    1. Smoke: curl https://" + app + ".fly.dev/health")
    print(f"    2. Share the MCP URL + AFAIR_AUTH_TOKEN with {args.github_username}")
    print("    3. (optional) point a vanity CNAME at this app via flyctl certs")


if __name__ == "__main__":
    main()
