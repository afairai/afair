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
2. Creates a new Fly app named ``afair-<name>-<suffix>`` where ``<name>``
   is a cosmic word (vega, polaris, lyra, …) and ``<suffix>`` is 3 hex
   chars, both derived deterministically from the user's GitHub
   username (avoids leaking the
   username in DNS while staying deterministic).
3. Creates a 1 GB volume named ``vault`` in the configured region.
4. Sets all required Fly secrets on the new app (per-user values
   above plus the shared GitHub OAuth client credentials). OAUTH_ISSUER
   is set to the BRANDED ``<name>-<suffix>.mcp.afair.ai`` URL so JWTs carry
   the right iss claim from day one.
5. Deploys the SAME afair image to the new app (no rebuild).
6. **Vanity domain**: creates ``<name>-<suffix>.mcp.afair.ai`` as a CNAME to
   ``<app>.fly.dev`` via the Cloudflare API, then registers it as a
   Fly cert host (Fly handles Let's-Encrypt issuance + renewal).
7. Appends the per-user secrets to .env.secrets.backup with the
   canonical annotation block (per global CLAUDE.md secrets-backup
   convention — secrets must NEVER live only in Fly).
8. Prints a one-page summary: vanity URL + fly.dev fallback, tokens,
   next steps.

What it does NOT do
===================
- It does NOT add the user to the master billing account; that's a
  separate Stripe webhook flow on afair-web that calls this script.
- It does NOT enable hourly snapshots per-user yet (see
  docs/operations.md §7). Default Fly daily snapshots apply.

Required environment
====================
- ``CLOUDFLARE_API_TOKEN``       (Zone:Read + Zone:DNS:Edit on afair.ai)
- ``GITHUB_OAUTH_CLIENT_ID``     (shared across users, set in shell)
- ``GITHUB_OAUTH_CLIENT_SECRET`` (shared across users, set in shell)
- ``FLY_API_TOKEN``              (used implicitly by flyctl)

Usage
=====

    uv run python scripts/provision_user.py <clerk_user_id>
    uv run python scripts/provision_user.py <clerk_user_id> --region fra
    uv run python scripts/provision_user.py <clerk_user_id> --dry-run

Idempotency
===========
Re-running with the same identity detects the existing app
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


# ── vanity domain (Cloudflare DNS + Fly cert) ─────────────────────────────


CLOUDFLARE_ZONE_ID = "1ce013da775674ceeb20d5e39ae407e7"
"""Zone ID for afair.ai on Cloudflare. Public-config not secret; leaks
nothing (zone IDs are random and per-zone, but anyone who controls the
domain can find them via API). Hardcoded here so the script doesn't
need to round-trip a zone-lookup on every run."""

VANITY_PARENT = "mcp.afair.ai"
"""Per-user vanity subdomains live under this parent. A user named
``gowry`` lands on ``vega-7a3.mcp.afair.ai``, CNAMEd to the matching
Fly app. Fly handles TLS termination."""


# Curated star + cosmic-object names. ~60 entries means together with
# the 3-hex suffix below we get 60 * 16^3 ≈ 245k unique vanity hosts
# before any second-order collision check is needed. Names chosen to
# read well, none over 8 chars, all unambiguous lowercase ASCII so DNS
# is friendly.
COSMIC_NAMES: tuple[str, ...] = (
    "altair",
    "andromeda",
    "antares",
    "arcturus",
    "aurora",
    "bellatrix",
    "betelgeuse",
    "canopus",
    "capella",
    "carina",
    "cassiopeia",
    "castor",
    "centauri",
    "cosmos",
    "crux",
    "cygnus",
    "deneb",
    "draco",
    "elara",
    "eridani",
    "europa",
    "fornax",
    "ganymede",
    "halley",
    "helios",
    "hydra",
    "io",
    "kepler",
    "lyra",
    "mira",
    "nebula",
    "nova",
    "oort",
    "orbit",
    "orion",
    "pavo",
    "pegasus",
    "perseus",
    "phoenix",
    "pluto",
    "polaris",
    "procyon",
    "pulsar",
    "quasar",
    "regulus",
    "rigel",
    "saturn",
    "sirius",
    "solis",
    "stardust",
    "supernova",
    "taurus",
    "titan",
    "vega",
    "vela",
    "virgo",
    "vortex",
    "voyager",
    "zenith",
    "zodiac",
)


def vanity_host_for(identity: str) -> str:
    """Per-user vanity hostname. Pick a cosmic word + 3-hex suffix
    deterministically from the user's identity (Clerk userId by
    default; legacy GitHub usernames also work since the hashing only
    cares about stable input bytes).

    ``user_abc123`` → ``vega-7a3.mcp.afair.ai`` (example shape; actual
    hash decides which name + suffix lands).

    The deterministic mapping means re-provisioning the same user
    yields the same hostname, which is critical because the JWT
    issuer + the certificate + the DNS record all reference it.
    """
    digest = hashlib.sha256(identity.lower().encode()).hexdigest()
    # First 4 hex chars (16 bits) pick a name out of COSMIC_NAMES.
    # Modular index space is plenty: 65k → 60 names, well distributed.
    name = COSMIC_NAMES[int(digest[:4], 16) % len(COSMIC_NAMES)]
    # Next 3 hex chars (12 bits) give 4096 variants per name.
    suffix = digest[4:7]
    return f"{name}-{suffix}.{VANITY_PARENT}"


# ── per-user identity ──────────────────────────────────────────────────────


def app_name_for(identity: str) -> str:
    """Deterministic per-user Fly app name. Uses the same name + suffix
    pair as the vanity host so the two are visually linked when reading
    a ``fly apps list``. The ``afair-`` prefix scopes the apps to this
    product inside the org."""
    digest = hashlib.sha256(identity.lower().encode()).hexdigest()
    name = COSMIC_NAMES[int(digest[:4], 16) % len(COSMIC_NAMES)]
    suffix = digest[4:7]
    return f"afair-{name}-{suffix}"


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


def fly_add_cert(app: str, hostname: str, *, dry: bool) -> None:
    """Tell Fly to issue + manage a TLS cert for ``hostname`` on ``app``.

    Fly does Let's-Encrypt issuance + renewal automatically once DNS
    is pointed at the app. Safe to call multiple times — Fly is
    idempotent on existing cert requests.
    """
    _run_fly(["certs", "add", hostname, "-a", app], dry=dry)


# ── Cloudflare DNS shim ───────────────────────────────────────────────────


def cloudflare_create_cname(
    *,
    hostname: str,
    target: str,
    token: str,
    dry: bool,
) -> None:
    """Create a CNAME record on the afair.ai zone.

    Idempotent: if a record with this name already exists we PATCH the
    target instead of POSTing a duplicate. Saves us from re-running
    provisioning failing with "DNS record already exists" mid-flow.
    """
    import json as _json
    import urllib.request

    short_name = hostname.removesuffix(f".{VANITY_PARENT.split('.', 1)[1]}")
    # ^ "vega-7a3.mcp.afair.ai" → "vega-7a3.mcp"
    # Cloudflare wants the leaf relative-to-zone-root (afair.ai), so we
    # strip the apex "afair.ai" off the end.
    print(f"  $ cloudflare DNS: {short_name} CNAME {target}")
    if dry:
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Check for existing record by name.
    list_url = (
        f"https://api.cloudflare.com/client/v4/zones/{CLOUDFLARE_ZONE_ID}"
        f"/dns_records?name={hostname}&type=CNAME"
    )
    req = urllib.request.Request(list_url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        listing = _json.loads(resp.read().decode("utf-8"))
    existing = listing.get("result", [])

    body = {
        "type": "CNAME",
        "name": hostname,
        "content": target,
        "ttl": 1,  # 1 = automatic (Cloudflare-managed, ~5 min)
        "proxied": False,  # critical: Fly needs the real target to issue a cert
        "comment": "managed by scripts/provision_user.py",
    }

    if existing:
        record_id = existing[0]["id"]
        update_url = (
            f"https://api.cloudflare.com/client/v4/zones/{CLOUDFLARE_ZONE_ID}"
            f"/dns_records/{record_id}"
        )
        req = urllib.request.Request(
            update_url,
            data=_json.dumps(body).encode("utf-8"),
            headers=headers,
            method="PUT",
        )
    else:
        create_url = f"https://api.cloudflare.com/client/v4/zones/{CLOUDFLARE_ZONE_ID}/dns_records"
        req = urllib.request.Request(
            create_url,
            data=_json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = _json.loads(resp.read().decode("utf-8"))

    if not result.get("success"):
        msg = f"Cloudflare API rejected the DNS write: {result.get('errors')}"
        raise RuntimeError(msg)


# ── secrets backup write ──────────────────────────────────────────────────


def append_secrets_backup(*, app: str, identity: str, s: UserSecrets) -> None:
    """Append the per-user secrets block to .env.secrets.backup.

    Format follows the global CLAUDE.md secrets-backup convention:
    each secret is annotated with where it's placed, when it was
    created, and how to rotate.
    """
    from datetime import UTC, datetime

    today = datetime.now(UTC).date().isoformat()
    block = f"""

# ───── PER-USER PROVISIONING — {app} ({identity}) ─────
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
    identity: str,
    region: str,
    dry: bool,
    enable_signup_token: bool,
) -> tuple[str, str, UserSecrets]:
    """Provision the user's app + DNS + cert. Returns (app, vanity_host, secrets).

    `identity` is the stable identifier the per-user MCP server will
    treat as the JWT subject — typically the Clerk userId. Legacy
    GitHub usernames also work since the hashing only cares about
    stable input bytes; bring-your-own works as long as the same
    string is fed in every time.
    """
    import os

    app = app_name_for(identity)
    vanity = vanity_host_for(identity)
    print(f"== Provisioning user '{identity}' ==")
    print(f"   Fly app:      {app}")
    print(f"   Vanity host:  {vanity}")
    print(f"   Fallback URL: {app}.fly.dev")

    if not dry and fly_app_exists(app):
        print(f"ERROR: app '{app}' already exists — refusing to clobber", file=sys.stderr)
        sys.exit(2)

    if dry:
        print("  (dry run — no remote calls)")

    # CF token is required for the DNS step. Read upfront so we fail
    # fast before any Fly state is created.
    cloudflare_token = os.environ.get("CLOUDFLARE_API_TOKEN")
    if not dry and not cloudflare_token:
        print(
            "ERROR: env var CLOUDFLARE_API_TOKEN must be set "
            "(scope: Zone:Read + Zone:DNS:Edit on afair.ai)",
            file=sys.stderr,
        )
        sys.exit(2)

    s = UserSecrets.fresh()
    secrets_map: dict[str, str] = {
        "AFAIR_AUTH_TOKEN": s.auth_token,
        "AFAIR_JWT_SECRET": s.jwt_secret,
        # OAUTH_ISSUER points at the VANITY URL — JWTs carry the
        # branded iss claim, well-known metadata advertises the
        # branded URL. The fly.dev fallback still resolves to the
        # same app for clients that bypass DNS.
        "OAUTH_ISSUER": f"https://{vanity}",
        # IDENTITY_ALLOWLIST is the set of JWT subjects the MCP server
        # will accept. Single entry per box (single-tenant by Invariant
        # I8). The identity here is the Clerk userId; the per-user
        # server's JWT verifier compares the token's `sub` claim against
        # this string.
        "IDENTITY_ALLOWLIST": identity,
        "ENVIRONMENT": "fly",
    }
    if enable_signup_token:
        secrets_map["AFAIR_SIGNUP_TOKEN"] = s.signup_token

    # Identity-hub secret is shared across all per-user MCP servers —
    # they verify identity-tokens minted by afair-web's hub against
    # this secret, and the hub uses the same value to sign. The
    # operator must export this in the shell before running this
    # script. GitHub OAuth client_id/secret are not used any more —
    # Clerk owns the actual authentication step.
    for shared_key in ("IDENTITY_HUB_SECRET",):
        v = os.environ.get(shared_key)
        if not v:
            print(
                f"ERROR: env var {shared_key} must be set before running",
                file=sys.stderr,
            )
            sys.exit(2)
        secrets_map[shared_key] = v

    # 1. Fly app + volume + secrets + deploy.
    fly_create_app(app, dry=dry)
    fly_create_volume(app, region=region, size_gb=DEFAULT_VOLUME_SIZE_GB, dry=dry)
    fly_set_secrets(app, secrets_map, dry=dry)
    fly_deploy_image(app, image=DOCKER_IMAGE, dry=dry)

    # 2. DNS — CNAME <name>-<suffix>.mcp.afair.ai → <app>.fly.dev.
    cloudflare_create_cname(
        hostname=vanity,
        target=f"{app}.fly.dev",
        token=cloudflare_token or "<dry-run>",
        dry=dry,
    )

    # 3. Fly cert — Fly handles Let's-Encrypt issuance once DNS resolves.
    #    Propagation typically takes ~30 s to ~5 min. The user can hit
    #    the fly.dev fallback URL immediately; the vanity URL becomes
    #    live as soon as cert + DNS both ready.
    fly_add_cert(app, vanity, dry=dry)

    if not dry:
        append_secrets_backup(app=app, identity=identity, s=s)

    return app, vanity, s


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument(
        "identity",
        help=(
            "Stable user identifier — the Clerk userId in production. "
            "Anything stable works (legacy GitHub usernames included) "
            "as long as the same string is used on every re-run."
        ),
    )
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

    app, vanity, s = provision(
        identity=args.identity,
        region=args.region,
        dry=args.dry_run,
        enable_signup_token=not args.no_signup_token,
    )

    print()
    print("=" * 64)
    print(f"  Provisioned for {args.identity}")
    print("=" * 64)
    print(f"  Primary URL:    https://{vanity}                  (becomes")
    print("                  available ~5 min after DNS propagates +")
    print("                  Fly Let's-Encrypt cert is ready)")
    print(f"  Fallback URL:   https://{app}.fly.dev               (works immediately)")
    print()
    print(f"  MCP endpoint:   https://{vanity}/mcp")
    print(f"  Health probe:   https://{vanity}/health")
    print(f"  OAuth metadata: https://{vanity}/.well-known/oauth-authorization-server")
    print()
    print("  Tokens (also persisted in .env.secrets.backup):")
    print(f"    AFAIR_AUTH_TOKEN   = {s.auth_token}")
    print(f"    AFAIR_JWT_SECRET   = {s.jwt_secret}")
    print(f"    AFAIR_SIGNUP_TOKEN = {s.signup_token}")
    print()
    print("  Next steps:")
    print(f"    1. Smoke (fallback URL works now): curl https://{app}.fly.dev/health")
    print(f"    2. Send onboarding email to user '{args.identity}'")
    print(f"    3. Confirm vanity URL is up: curl https://{vanity}/health (~5 min)")


if __name__ == "__main__":
    main()
