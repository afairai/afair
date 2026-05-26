"""Tests for the Claude Code SessionStart lifecycle hook.

Phase 2 #3 — Cognee steal, implemented as a stock-Claude-Code hook
script (no plugin SDK required). The script lives at
``scripts/claude_code_hooks/session_start.py`` and emits JSON to stdout
that Claude Code parses as ``hookSpecificOutput.additionalContext``.

These tests exercise the script as a subprocess so the actual stdout
contract is verified end-to-end. Network is mocked by pointing
NEVERFORGET_URL at a tiny local HTTP server we spin up here.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from collections.abc import Iterator

HOOK_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "claude_code_hooks" / "session_start.py"
)


# ── tiny fake MCP server ──────────────────────────────────────────────────


class _FakeMCP(BaseHTTPRequestHandler):
    """Just enough JSON-RPC to satisfy initialize + tools/call list_context.

    The class-level ``response_for`` dict lets tests override responses.
    """

    response_for: ClassVar[dict[str, dict]] = {}

    def log_message(self, *_: object, **__: object) -> None:
        """Suppress noisy default access-log spam in pytest output."""

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}

        method = payload.get("method", "")
        request_id = payload.get("id")
        body = self._handle(method, request_id, payload)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("mcp-session-id", "fake-session")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode("utf-8"))

    def _handle(self, method: str, request_id: object, _payload: dict) -> dict:
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"protocolVersion": "2025-03-26", "capabilities": {}},
            }
        if method == "notifications/initialized":
            return {}
        if method == "tools/call":
            override = type(self).response_for.get("tools/call")
            if override is not None:
                return {"jsonrpc": "2.0", "id": request_id, "result": override}
            # Post-collapse RecallResult shape: hits at top level, summary as sibling
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "structuredContent": {
                        "hits": [
                            {
                                "event_id": "01ABCDEF0123456789",
                                "content_hash": "sha256:deadbeef",
                                "created_at": "2026-05-26T00:00:00Z",
                                "kind": "remember",
                                "origin": "user",
                                "payload": {
                                    "content_type": "text",
                                    "text": "hello world",
                                },
                                "truncated": False,
                                "interpretation": {
                                    "best_guess_kind": "fact",
                                    "summary": "A canary fact saved at midnight.",
                                },
                                "linked_event_ids": [],
                                "parent_hashes": [],
                            },
                        ],
                        "depth_used": "shallow",
                        "summary": {
                            "total_events": 3,
                            "by_kind": {"remember": 2, "observe": 1},
                            "by_origin": {"user": 3},
                        },
                    }
                },
            }
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601}}


@contextmanager
def _fake_server() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _FakeMCP)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.shutdown()
        server.server_close()


def _run_hook(env_extra: dict[str, str], stdin: str = "{}") -> dict:
    """Invoke the hook script as a subprocess; parse its JSON stdout."""
    # HOME defaults to a non-existent path so the file-fallback discovery
    # ('~/.neverforget.env') doesn't pick up the developer's real config.
    # Tests that exercise the file path explicitly override HOME.
    env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent-home-for-hook-tests", **env_extra}
    proc = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert proc.returncode == 0, f"hook exited {proc.returncode}; stderr={proc.stderr!r}"
    return json.loads(proc.stdout)


# ── tests ──────────────────────────────────────────────────────────────────


def test_hook_emits_valid_json_when_no_token_present() -> None:
    """Without credentials the hook MUST still emit valid JSON so Claude
    Code doesn't log a parse error. The session continues without any
    vault context."""
    out = _run_hook(env_extra={})  # no NEVERFORGET_AUTH_TOKEN
    assert out == {"continue": True}


def test_hook_emits_valid_json_when_server_unreachable() -> None:
    """A configured but unreachable server must fail silently — the
    user's Claude Code session should never break because the memory
    layer is down."""
    out = _run_hook(
        env_extra={
            "NEVERFORGET_URL": "http://127.0.0.1:1/mcp",  # connection refused
            "NEVERFORGET_AUTH_TOKEN": "anything",
        }
    )
    assert out == {"continue": True}


def test_hook_returns_additional_context_with_vault_summary() -> None:
    """Happy path — server reachable + token valid + vault has events.
    The hook outputs hookSpecificOutput.additionalContext with a
    markdown brief that the AI will see as part of the session prompt."""
    _FakeMCP.response_for = {}  # use default response
    with _fake_server() as url:
        out = _run_hook(env_extra={"NEVERFORGET_URL": url, "NEVERFORGET_AUTH_TOKEN": "tok"})
    assert out["continue"] is True
    hook_out = out["hookSpecificOutput"]
    assert hook_out["hookEventName"] == "SessionStart"
    ctx = hook_out["additionalContext"]
    assert "Vault context" in ctx
    assert "3 events" in ctx
    assert "A canary fact saved at midnight" in ctx
    assert "01ABCDEF0123456789" in ctx


def test_hook_skips_when_vault_is_empty() -> None:
    """Empty vault — no point polluting the session prompt with
    'You have 0 events'. The hook emits the silent-ok shape."""
    _FakeMCP.response_for = {
        "tools/call": {
            "structuredContent": {
                "hits": [],
                "depth_used": "shallow",
                "summary": {
                    "total_events": 0,
                    "by_kind": {},
                    "by_origin": {},
                },
            }
        }
    }
    try:
        with _fake_server() as url:
            out = _run_hook(env_extra={"NEVERFORGET_URL": url, "NEVERFORGET_AUTH_TOKEN": "tok"})
    finally:
        _FakeMCP.response_for = {}
    assert out == {"continue": True}


def test_hook_marks_invalidated_events_in_summary() -> None:
    """An event with an invalidation field gets struck-through text in
    the markdown summary so the AI sees current-vs-historical at a glance."""
    _FakeMCP.response_for = {
        "tools/call": {
            "structuredContent": {
                "hits": [
                    {
                        "event_id": "01XYZ",
                        "content_hash": "sha256:zzz",
                        "created_at": "2026-05-26T00:00:00Z",
                        "kind": "remember",
                        "origin": "user",
                        "payload": {"content_type": "text", "text": "old fact"},
                        "truncated": False,
                        "interpretation": {"summary": "Sajinth is CEO"},
                        "linked_event_ids": [],
                        "parent_hashes": [],
                        "invalidation": {
                            "at": "2026-05-26T01:00:00Z",
                            "by_event_id": "01ABC",
                            "reason": "he stepped down",
                        },
                    }
                ],
                "depth_used": "shallow",
                "summary": {
                    "total_events": 1,
                    "by_kind": {"remember": 1},
                    "by_origin": {"user": 1},
                },
            }
        }
    }
    try:
        with _fake_server() as url:
            out = _run_hook(env_extra={"NEVERFORGET_URL": url, "NEVERFORGET_AUTH_TOKEN": "tok"})
    finally:
        _FakeMCP.response_for = {}
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "~~Sajinth is CEO~~" in ctx, ctx
    assert "(invalidated)" in ctx


def test_hook_reads_token_from_neverforget_env_file(tmp_path: Path) -> None:
    """Fallback path — when env vars are missing, hook reads
    ~/.neverforget.env (the file the installer writes). We override
    HOME to a temp dir to keep the test hermetic."""
    config = tmp_path / ".neverforget.env"
    with _fake_server() as url:
        config.write_text(f"NEVERFORGET_URL={url}\nNEVERFORGET_AUTH_TOKEN=tok\n")
        out = _run_hook(env_extra={"HOME": str(tmp_path)})
    assert out["continue"] is True
    assert "Vault context" in out["hookSpecificOutput"]["additionalContext"]


def test_hook_consumes_stdin_without_blocking() -> None:
    """Claude Code pipes a JSON payload to the hook on stdin (for some
    hook events). We read and discard — never block waiting for input
    that may not arrive."""
    # Pass a non-empty stdin to make sure the read doesn't trip us up.
    out = _run_hook(env_extra={}, stdin='{"some":"payload"}')
    assert out == {"continue": True}
