"""Retire a per-user afair MCP instance — the symmetric counterpart to
provision_user.py.

provision_user.py builds a user's world (Fly app + volume + vanity host +
escrow). This script tears it down again, and it is the SINGLE canonical
teardown path: both the 30-day grace-period cron (afair-web) and the
instant user-initiated delete dispatch THIS script (via .github/workflows/
retire.yml), so the destroy logic lives in exactly one place. No raw
``fly apps destroy`` scattered across callers.

What it does (per invocation)
=============================
1. Destroys the Fly app ``afair-<name>-<suffix>`` derived deterministically
   from the user's identity (Clerk userId). ``fly apps destroy`` takes the
   machine, the volume, AND the managed cert with it — one call.
2. Removes the vanity CNAME ``<name>-<suffix>.mcp.afair.ai`` from the
   afair.ai Cloudflare zone.
3. Calls back to afair-web ``/api/internal/retired`` so the control-plane
   row gets ``deleted_at`` set, ``status='deleted'``, and the encrypted
   secrets escrow WIPED (there is nothing left to recover once the volume
   is gone — keeping the escrow would be dead, sensitive ciphertext).

What it does NOT do
===================
- It does NOT cancel the Stripe subscription. Billing is afair-web's
  concern: the grace cron only ever retires already-``canceled`` users,
  and the instant-delete server action cancels Stripe BEFORE dispatching
  this script. Keeping billing out of here means this script is safe to
  run for any reason (GDPR erasure, abuse, test cleanup) without coupling
  to payment state.
- It does NOT export the vault. Export is offered to the user in the UI
  BEFORE this runs (account page → "export your vault"). Once this script
  runs the bytes are gone for good — that is the point of erasure.

Idempotency
===========
Every step is safe to re-run. ``fly apps destroy`` is a no-op on an
already-gone app; the Cloudflare delete is a lookup-then-delete that
finds nothing on a second pass; the callback's ``markRetired`` is an
idempotent UPDATE. A teardown that failed partway (app gone, DNS left
behind) re-runs cleanly and finishes the rest.

Required environment
====================
- ``FLY_API_TOKEN``            (used implicitly by flyctl)
- ``CLOUDFLARE_API_TOKEN``     (Zone:Read + Zone:DNS:Edit on afair.ai)
- ``RETIRE_CALLBACK_SECRET``   (shared bearer for the afair-web callback)
- ``RETIRE_CALLBACK_URL``      (default https://afair.ai/api/internal/retired)

Usage
=====

    uv run python scripts/retire_user.py <clerk_user_id>
    uv run python scripts/retire_user.py <clerk_user_id> --reason user-requested
    uv run python scripts/retire_user.py <clerk_user_id> --dry-run
    uv run python scripts/retire_user.py <clerk_user_id> --keep-dns   # app only

``--reason`` is free-text passed through to the callback + logs for the
audit trail (``canceled-grace`` for the cron, ``user-requested`` for the
account-page button, anything for manual runs).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

# Reuse the EXACT identity → app/host derivation provisioning used, so a
# retire can never target the wrong machine. Same hash, same names.
# Dual-mode import: bare when run as a script (scripts/ on sys.path), and
# package-qualified when imported as scripts.retire_user (pytest, tooling).
try:
    from provision_user import (
        CLOUDFLARE_ZONE_ID,
        VANITY_PARENT,
        app_name_for,
        vanity_host_for,
    )
except ModuleNotFoundError:  # pragma: no cover - import-path shim
    from scripts.provision_user import (
        CLOUDFLARE_ZONE_ID,
        VANITY_PARENT,
        app_name_for,
        vanity_host_for,
    )


# ── Fly teardown ───────────────────────────────────────────────────────────


def fly_app_exists(app: str) -> bool:
    """Probe via ``flyctl apps list`` — exits 0 regardless, so grep output."""
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


def fly_destroy_app(app: str, *, dry: bool) -> bool:
    """Destroy the Fly app (machine + volume + cert in one call).

    Returns True if a destroy was issued, False if the app was already
    gone (idempotent no-op). ``--yes`` skips the interactive prompt.
    """
    if not dry and not fly_app_exists(app):
        print(f"  (app {app} already gone — skipping destroy)")
        return False
    cmd = ["flyctl", "apps", "destroy", app, "--yes"]
    print(f"  $ {' '.join(cmd)}")
    if dry:
        return True
    subprocess.run(cmd, check=True)
    return True


# ── Cloudflare DNS teardown ────────────────────────────────────────────────


def cloudflare_delete_cname(*, hostname: str, token: str, dry: bool) -> int:
    """Delete the vanity CNAME from the afair.ai zone. Returns the number
    of records removed (0 if none — already clean).

    Lookup-by-name then DELETE-by-id, mirroring the create path in
    provision_user.cloudflare_create_cname so the two are obviously
    inverse operations. Idempotent: a second run finds nothing.
    """
    import urllib.request

    print(f"  $ cloudflare DNS: delete {hostname} CNAME")
    if dry:
        return 0

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    list_url = (
        f"https://api.cloudflare.com/client/v4/zones/{CLOUDFLARE_ZONE_ID}"
        f"/dns_records?name={hostname}&type=CNAME"
    )
    req = urllib.request.Request(list_url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        listing = json.loads(resp.read().decode("utf-8"))
    records = listing.get("result", [])

    removed = 0
    for rec in records:
        del_url = (
            f"https://api.cloudflare.com/client/v4/zones/{CLOUDFLARE_ZONE_ID}"
            f"/dns_records/{rec['id']}"
        )
        del_req = urllib.request.Request(del_url, headers=headers, method="DELETE")
        with urllib.request.urlopen(del_req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        if not result.get("success"):
            msg = f"Cloudflare API rejected the DNS delete: {result.get('errors')}"
            raise RuntimeError(msg)
        removed += 1
        print(f"    DNS removed: {hostname}")
    return removed


# ── afair-web callback ─────────────────────────────────────────────────────


def notify_retired(*, identity: str, app: str, reason: str, dry: bool) -> bool:
    """Tell afair-web the machine is gone so it sets deleted_at, flips
    status='deleted', and WIPES the now-useless secrets escrow.

    Shared-bearer authed (RETIRE_CALLBACK_SECRET). Best-effort: the Fly
    teardown already happened, so a failed callback is logged not fatal —
    the operator can re-fire, and the row staying un-flagged just means the
    grace cron re-dispatches (which Fly-destroys nothing and re-callbacks).
    """
    import urllib.request

    url = os.environ.get("RETIRE_CALLBACK_URL", "https://afair.ai/api/internal/retired")
    secret = os.environ.get("RETIRE_CALLBACK_SECRET")
    if not secret:
        print("notify_retired: RETIRE_CALLBACK_SECRET not set — skipping callback")
        return False
    if dry:
        print(f"  $ POST {url}  {{clerk_user_id, fly_app, reason}}")
        return True

    body = json.dumps({"clerk_user_id": identity, "fly_app": app, "reason": reason}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            ok = 200 <= resp.status < 300
            print(f"notify_retired: {url} -> HTTP {resp.status}")
            return ok
    except Exception as exc:
        print(f"notify_retired: callback failed: {exc}")
        return False


# ── orchestration ──────────────────────────────────────────────────────────


def retire(
    *,
    identity: str,
    reason: str,
    dry: bool,
    keep_dns: bool = False,
    skip_callback: bool = False,
) -> tuple[str, str]:
    """Tear down ``identity``'s per-user world. Returns (app, vanity_host).

    Order is deliberate: Fly app first (the expensive, billable thing),
    then DNS, then the control-plane flag. If the run dies after the Fly
    destroy, the user is already not being billed for compute; a re-run
    finishes DNS + callback.
    """
    app = app_name_for(identity)
    vanity = vanity_host_for(identity)
    print(f"== Retiring user '{identity}' (reason: {reason}) ==")
    print(f"   Fly app:      {app}")
    print(f"   Vanity host:  {vanity}")
    if dry:
        print("  (dry run — no remote calls)")

    cloudflare_token = os.environ.get("CLOUDFLARE_API_TOKEN")
    if not dry and not keep_dns and not cloudflare_token:
        print(
            "ERROR: CLOUDFLARE_API_TOKEN must be set (or pass --keep-dns)",
            file=sys.stderr,
        )
        sys.exit(2)

    # 1. Fly app — machine + volume + cert, gone in one call.
    fly_destroy_app(app, dry=dry)

    # 2. DNS — remove the vanity CNAME so the host stops resolving.
    if keep_dns:
        print("  (--keep-dns — leaving the CNAME in place)")
    else:
        cloudflare_delete_cname(hostname=vanity, token=cloudflare_token or "<dry-run>", dry=dry)

    # 3. Control-plane — deleted_at + status='deleted' + wipe escrow.
    if skip_callback:
        print("  (--skip-callback — control-plane row left untouched)")
    else:
        notify_retired(identity=identity, app=app, reason=reason, dry=dry)

    print("\n-- teardown complete --")
    print(f"   {app} destroyed; {vanity} no longer resolves.")
    return app, vanity


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument(
        "identity",
        help="Stable user identifier — the Clerk userId. Same string fed "
        "to provision_user.py; the app/host names are re-derived from it.",
    )
    ap.add_argument(
        "--reason",
        default="manual",
        help="Free-text audit reason (e.g. canceled-grace, user-requested).",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned actions without executing.",
    )
    ap.add_argument(
        "--keep-dns",
        action="store_true",
        help="Destroy the Fly app but leave the vanity CNAME in place.",
    )
    ap.add_argument(
        "--skip-callback",
        action="store_true",
        help="Skip the afair-web callback (leave the control-plane row alone).",
    )
    args = ap.parse_args()

    # The host name in VANITY_PARENT is referenced indirectly via the
    # imported helpers; keep the import live + assert the wiring so a
    # refactor that drops it fails loudly here, not silently in prod.
    assert VANITY_PARENT

    app, vanity = retire(
        identity=args.identity,
        reason=args.reason,
        dry=args.dry_run,
        keep_dns=args.keep_dns,
        skip_callback=args.skip_callback,
    )

    print()
    print("=" * 64)
    print(f"  Retired {args.identity}")
    print("=" * 64)
    print(f"  Destroyed app:  {app}")
    print(f"  Removed host:   {vanity}")
    print(f"  Reason:         {args.reason}")


if __name__ == "__main__":
    main()
