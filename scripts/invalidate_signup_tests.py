"""
One-shot: invalidate test/junk early-access signup events in the vault.

Rationale: the previous signup flow wrote every form submission as a
``remember()`` event into Gowry's personal substrate. That included
deployment smoketests, hardening-pass synthetic signups, and one
junk entry ("lala@lala.com"). Those should not show up in any future
recall over the early-access list.

Approach: call ``remember(invalidates=[hash, ...])`` once with all
the test hashes. afair semantics: the new event supersedes the old
ones; recall with default ``include_invalidated=False`` filters them
out of survey results. The original rows stay in the DB (append-only
substrate) but are hidden from the operational view.

Hashes were captured by querying the live vault via MCP recall on
2026-06-01 and are checked into the script as a literal list so the
operation is auditable.

Run (from project root, with venv active):
    python scripts/invalidate_signup_tests.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

# ─── test entries to invalidate ────────────────────────────────────────────
#
# Captured 2026-06-01 from `afair.recall(query="afair.ai early-access
# signup")` on the live mcp.afair.ai. Email addresses are kept inline so
# anyone reviewing this diff can verify the classification ("is this
# really a test or a real signup?") without needing the vault.
TEST_ENTRIES: list[tuple[str, str]] = [
    ("sha256:12b0452e011e993733228e264c3d5f46ee042d5d48143b56947af96a3039cc05", "lala@lala.com"),
    ("sha256:30c8283722864c4ca3b2a3344f9dc92929fbbd5d08d4d434223f3204ef1c3d74", "smoketest-final-phase-b@afair.ai"),
    ("sha256:97756cc12c8f4bd1cb650537d9a43c8f0e6ace1ee8ec4fc1d72f568cd1f67471", "smoketest-input-bounds@afair.ai"),
    ("sha256:ae69e1816b20eac62c4994c224f0f4ed52e8e43125344e116476cdb675a866aa", "smoketest-rollback@afair.ai"),
    ("sha256:d41a6eed82f1ef0d358b6e25aaa859c877efc140289335ff31d29e34894d01af", "smoketest-i7-roundtrip@afair.ai"),
    ("sha256:e7096e35633fe93054848219b1498d8b0a0e3390a95c101382bf0015f10eb78d", "asgi-smoke-1780170810@example.com"),
    ("sha256:11d49ef9788e06d2185751fc9f2331cc57ab92d564518c665c25612967e66a60", "asgi-deploy-verify-1780171183@example.com"),
    ("sha256:af4935e225593cc92bd74a80e9fbd015c70af7eb70675d273cc593d5405bc4c3", "final-smoke-1780172359@example.com"),
    ("sha256:e99a490f1f107f6c1ffb82f74716d323ac5224f8d8e0398c6741b95a2eac7c94", "perf-minors-1780173310@example.com"),
    ("sha256:ce259a45f9b3bc7a53eb5bbab8ea13a6ac4cc3c1ae5053b3456fb4db02710ba0", "multi-modal-smoke-1780178570@example.com"),
    ("sha256:937baa5b8bdabf3496c5def64bb7f4346bc5ae8ade602ad94b997ace91016e93", "phase-2-agents-live-1780199101@example.com"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Invalidate test signup events.")
    parser.add_argument("--dry-run", action="store_true", help="Print the request but do not send it.")
    parser.add_argument(
        "--url",
        default=os.environ.get("AFAIR_MCP_URL", "https://mcp.afair.ai/mcp"),
        help="MCP endpoint (default: $AFAIR_MCP_URL or https://mcp.afair.ai/mcp).",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("AFAIR_AUTH_TOKEN"),
        help="Bearer token (default: $AFAIR_AUTH_TOKEN).",
    )
    args = parser.parse_args()

    if not args.token and not args.dry_run:
        print("AFAIR_AUTH_TOKEN not set and not in --dry-run.", file=sys.stderr)
        return 2

    hashes = [h for h, _ in TEST_ENTRIES]
    print(f"Invalidating {len(hashes)} test entries:")
    for h, email in TEST_ENTRIES:
        print(f"  {h[:24]}…  {email}")

    # MCP tool call envelope. The remember tool accepts:
    #   content: {type:"text", text:<note>}
    #   context: free-form
    #   type_hint: free-form
    #   invalidates: [content_hash, ...]
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "remember",
            "arguments": {
                "content": {
                    "type": "text",
                    "text": (
                        "Cleanup 2026-06-01: invalidating early-access signup "
                        "events that were never real signups (deployment "
                        "smoketests, hardening-pass synthetic addresses, one "
                        "junk submission). The signup list source-of-truth "
                        "moved off the vault to afair-web's Postgres table; "
                        "these entries should not surface in any future "
                        "recall over the list."
                    ),
                },
                "context": "afair-web signup cleanup",
                "type_hint": "decision",
                "invalidates": hashes,
            },
        },
    }

    body = json.dumps(payload).encode("utf-8")

    if args.dry_run:
        print("\n--dry-run: would POST to", args.url)
        print(json.dumps(payload, indent=2))
        return 0

    req = urllib.request.Request(
        args.url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {args.token}",
            "User-Agent": "afair-cleanup/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            ct = resp.headers.get("Content-Type", "")
            raw = resp.read().decode("utf-8", errors="replace")
            print(f"\nResponse {resp.status} ({ct}):")
            # Streamable HTTP returns SSE in some configurations; pretty-print
            # JSON if that's what came back, otherwise dump raw.
            if "application/json" in ct:
                print(json.dumps(json.loads(raw), indent=2))
            else:
                print(raw)
        return 0
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} — {e.reason}\n{e.read().decode('utf-8', errors='replace')}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"Failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
