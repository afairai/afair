"""Onboarding email, sent from the provisioning job once a user's machine
is up.

Lives here (not in afair-web) on purpose: the email carries the user's
freshly minted AFAIR_AUTH_TOKEN, and the rule is that afair-web never sees
the master token. provision_user.py mints it, sends this mail, and the
token never leaves the provisioning process.

MailerSend over plain HTTPS (urllib, no SDK). Requires:
  MAILERSEND_API_TOKEN   transactional send token
  MAILERSEND_FROM_EMAIL  verified sender (e.g. hello@afair.ai)
  MAILERSEND_FROM_NAME   optional display name
"""

from __future__ import annotations

import json
import os
import urllib.request

# The canonical AI-instruction snippet, byte-identical to the one in
# CLAUDE.md and the MCP onboarding resource. Kept inline so the email is
# self-contained.
_SNIPPET = """\
## afair — Persistent Memory Across AI Tools

You have access to `afair`, an MCP server that holds my durable memory
across every session, every AI tool, every device. Use it. It is not
optional.

When to call:
- Always at conversation start: call afair.recall(query="<topic>") to
  surface relevant past context.
- Whenever I share something durable (a decision, a preference, a fact,
  a deadline, a commitment, an insight): call afair.remember(...).
- Whenever you take an action (edit a file, run a query, call a tool):
  call afair.observe(...).

I installed afair so my memory doesn't reset every chat. A session where
you have access to afair but don't call it is worse than one without it."""


def _body(vanity_host: str, auth_token: str) -> str:
    mcp_url = f"https://{vanity_host}/mcp"
    return f"""Hi,

Your afair is up.

Your dedicated machine is live in the EU (Frankfurt region). Two pieces
follow: how to connect, and a snippet that tells your AI how to use the
connection. You can always come back to https://afair.ai/account to copy
these again.

CONNECT, OPTION A: web AI tools (Claude.ai, ChatGPT, Claude Code, Codex,
Cursor with MCP UI). Add this URL as a custom connector and approve the
browser sign-in. No token to paste.

  MCP URL:   {mcp_url}

CONNECT, OPTION B: local mcp.json or CLI tools. Paste both the URL and the
bearer token, no OAuth round-trip needed.

  MCP URL:        {mcp_url}
  Bearer token:   {auth_token}

(Keep the token private. Anyone holding it can read your vault. Lost it?
Reply to this mail, it gets rotated within an hour.)

Paste the snippet below into your AI client's instructions (Claude.ai
Custom Instructions, Claude Code CLAUDE.md, Codex AGENTS.md, Cursor
rules):

---
{_SNIPPET}
---

A few things worth knowing:
- The first conversation produces no context, since the vault is empty.
  Talk to it as you normally would and it learns. After a week of normal
  use the recall results get meaningfully useful.
- Your machine is dedicated to you. No shared database. Every byte is
  exportable any time with the bearer token above:

      curl -H "Authorization: Bearer {auth_token}" \\
           "https://{vanity_host}/internal/export?blobs=inline" \\
           > afair-export.jsonl

- Hourly snapshots in the EU. If you do something unrecoverable, write in
  and it gets rolled back to a point in time.

Anything else, reply to this mail.

the afair team
afair.ai
Made in Germany
"""


def send_onboarding_email(*, email: str, vanity_host: str, auth_token: str) -> bool:
    """Send the onboarding email. Returns True on accepted send.

    Non-fatal by contract: provisioning already succeeded by the time this
    runs, so the caller logs a failure but does not unwind the machine.
    """
    api_token = os.environ.get("MAILERSEND_API_TOKEN")
    from_email = os.environ.get("MAILERSEND_FROM_EMAIL")
    from_name = os.environ.get("MAILERSEND_FROM_NAME", "afair")
    if not api_token or not from_email:
        print("onboarding_email: MAILERSEND_API_TOKEN / MAILERSEND_FROM_EMAIL not set")
        return False

    payload = {
        "from": {"email": from_email, "name": from_name},
        "to": [{"email": email}],
        "subject": "your afair is up",
        "text": _body(vanity_host, auth_token),
        "reply_to": {"email": from_email},
    }
    req = urllib.request.Request(
        "https://api.mailersend.com/v1/email",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            ok = 200 <= resp.status < 300
            print(f"onboarding_email: sent to {email} (HTTP {resp.status})")
            return ok
    except Exception as exc:
        print(f"onboarding_email: send failed: {exc}")
        return False
