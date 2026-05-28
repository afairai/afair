#!/usr/bin/env python3
"""Claude Code SessionStart hook — auto-loads vault context.

Stolen from Cognee's lifecycle-plugin pattern, implemented as a thin
shell hook so it works with stock Claude Code (no plugin SDK required).

What it does
------------
At the start of every Claude Code session (any directory), this hook:

  1. Reads the bearer token + URL for the user's afair instance
     from the environment OR from ``~/.afair.env`` if present.
  2. Calls the MCP ``recall(stats=True)`` tool over HTTP/JSON-RPC.
  3. Emits a JSON object Claude Code parses to inject the result into
     the new session's ``additionalContext``.

If the server is unreachable, the auth token is missing, or anything
else goes wrong, the hook fails silently — the session starts normally
without vault context. This is intentional: a broken afair should
never block the user from working.

Output format
-------------
Claude Code reads stdout looking for ``{"hookSpecificOutput": {...}}``.
For SessionStart we emit:

    {"hookSpecificOutput": {"hookEventName": "SessionStart",
                             "additionalContext": "<markdown>"}}

The additionalContext becomes part of the system context for the
session, so the AI starts every session aware of what's in the vault
without having to manually call ``recall``.

Installation
------------
The installer (``scripts/install_clients.py``) registers this script in
``~/.claude/settings.json`` under ``hooks.SessionStart``. To disable,
remove the entry there.

Config discovery
----------------
1. ``AFAIR_URL`` env var, default ``https://mcp.afair.ai/mcp``
2. ``AFAIR_AUTH_TOKEN`` env var
3. If env vars missing, falls back to ``~/.afair.env`` (gitignored
   file the installer writes when configured).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://mcp.afair.ai/mcp"
DEFAULT_CONFIG_PATH = Path.home() / ".afair.env"
TIMEOUT_SECONDS = 5.0
MAX_HITS_TO_SUMMARIZE = 8


def _load_config() -> tuple[str, str | None]:
    """Return (url, token). Env wins; file is fallback."""
    url = os.environ.get("AFAIR_URL")
    token = os.environ.get("AFAIR_AUTH_TOKEN")
    if not (url and token) and DEFAULT_CONFIG_PATH.exists():
        for line in DEFAULT_CONFIG_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key == "AFAIR_URL" and not url:
                url = value
            elif key == "AFAIR_AUTH_TOKEN" and not token:
                token = value
    return url or DEFAULT_URL, token


def _emit_silent_ok() -> None:
    """Hook contract — always emit valid JSON so Claude Code doesn't log
    a parse error. Empty additionalContext means 'no change'."""
    print(json.dumps({"continue": True}))


def _emit_context(context_md: str) -> None:
    print(
        json.dumps(
            {
                "continue": True,
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": context_md,
                },
            }
        )
    )


def _post(
    url: str, token: str, payload: dict, session_id: str | None = None
) -> tuple[str | None, dict]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["mcp-session-id"] = session_id
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as r:
        sid = r.headers.get("mcp-session-id")
        body = r.read().decode("utf-8", errors="replace")
    # Streamable HTTP can return SSE-style framed JSON. Parse the first JSON we see.
    for line in body.splitlines():
        if line.startswith("data: "):
            line = line[6:]
        line = line.strip()
        if line.startswith("{"):
            return sid, json.loads(line)
    return sid, {}


def _fetch_vault_summary(url: str, token: str) -> str | None:
    """Initialize an MCP session, call recall(stats=True), format the result.

    Returns the markdown summary or None on any failure. The caller
    treats None as "skip silently".
    """
    try:
        sid, _ = _post(
            url,
            token,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "claude-code-session-start", "version": "1"},
                },
            },
        )
        # Required handshake notification.
        _post(
            url,
            token,
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            session_id=sid,
        )
        _, body = _post(
            url,
            token,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "recall",
                    "arguments": {"stats": True, "limit": MAX_HITS_TO_SUMMARIZE},
                },
            },
            session_id=sid,
        )
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    except Exception:
        return None

    return _format_summary(body)


def _format_summary(body: dict) -> str | None:
    """Turn the recall(stats=True) result into a compact markdown brief.

    The new RecallResult shape carries ``summary`` (totals + breakdowns)
    and ``hits`` (recent events) — both populated when stats=True.
    """
    try:
        result = body["result"]["structuredContent"]
        summary = result.get("summary") or {}
        recent = result.get("hits") or []
    except (KeyError, TypeError):
        return None

    total = summary.get("total_events", 0)
    by_kind = summary.get("by_kind") or {}
    if total == 0:
        return None  # empty vault — don't pollute the session

    lines: list[str] = [
        "## Vault context (auto-loaded by afair)",
        "",
        f"You have access to a persistent memory vault with **{total} events**.",
    ]
    if by_kind:
        kind_summary = ", ".join(
            f"{n} {k}" for k, n in sorted(by_kind.items(), key=lambda x: -x[1])
        )
        lines.append(f"Breakdown: {kind_summary}.")
    lines.append("")

    if recent:
        lines.append(
            "**Most recent context** (use `recall(query=...)` to dig deeper, "
            "`recall(by_id=..., full_payload=True)` for full content):"
        )
        lines.append("")
        for hit in recent[:MAX_HITS_TO_SUMMARIZE]:
            interp = hit.get("interpretation") or {}
            kind = hit.get("kind") or "event"
            ev_id = hit.get("event_id") or ""
            ev_summary = interp.get("summary") or _payload_oneliner(hit.get("payload"))
            if hit.get("invalidation"):
                ev_summary = f"~~{ev_summary}~~ (invalidated)"
            lines.append(f"- `{ev_id}` ({kind}) — {ev_summary}")
        lines.append("")
    lines.append(
        "Call `recall(query)` for semantic + keyword retrieval over the full vault. "
        "Call `remember(content)` to save anything durable from this session."
    )
    return "\n".join(lines)


def _payload_oneliner(payload: dict | None) -> str:
    if not payload:
        return "(no preview)"
    ct = payload.get("content_type", "")
    if ct == "text":
        text = payload.get("text", "")
        return text[:120].replace("\n", " ") + ("…" if len(text) > 120 else "")
    if ct == "event":
        bits = [str(payload.get(k, "")) for k in ("action", "subject", "result") if payload.get(k)]
        return " · ".join(bits) or "(observe event)"
    if ct in {"binary", "text-large"}:
        return f"{ct} · {payload.get('mime', '?')} · {payload.get('size_bytes', '?')} bytes"
    return f"({ct})"


def main() -> int:
    # Read whatever Claude Code piped to stdin — we don't need it for
    # SessionStart but consuming the pipe is hygienic.
    with contextlib.suppress(Exception):
        sys.stdin.read()

    url, token = _load_config()
    if not token:
        _emit_silent_ok()
        return 0

    summary = _fetch_vault_summary(url, token)
    if summary is None:
        _emit_silent_ok()
        return 0

    _emit_context(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
