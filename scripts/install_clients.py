#!/usr/bin/env python
"""Install the neverforget MCP server into every detected MCP client.

Detects and configures (only when the client looks installed):

  - Claude Code  → ~/.claude/settings.json + ~/.claude/CLAUDE.md
  - Codex CLI    → ~/.codex/config.toml    + ~/.codex/AGENTS.md
  - Cursor       → ~/.cursor/mcp.json      + ~/.cursor/rules/neverforget.md
  - Claude.ai    → UI only; prints manual steps

Idempotent: running again replaces the existing neverforget entry and does
not duplicate the instruction snippet. Always backs up any file it changes
to ``<path>.bak.<timestamp>`` so revert is one ``mv`` away.

Usage:
    uv run python scripts/install_clients.py            # apply
    uv run python scripts/install_clients.py --dry-run  # preview only
    URL=... TOKEN=... uv run python scripts/install_clients.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ── config ──────────────────────────────────────────────────────────────────

SERVER_NAME = "neverforget"
DEFAULT_URL = "https://neverforget.fly.dev/mcp"

SNIPPET_MARKER = "## neverforget MCP"
SNIPPET_BODY = """\
You have access to a `neverforget` MCP server providing persistent memory
across sessions, AI clients, and devices. Use it daily:

1. **Before answering** questions that benefit from history (preferences,
   past decisions, names, ongoing projects, recurring themes), call
   `recall(query)` first.
2. **When the user signals** save/remember/note/keep, call `remember()`.
   Also call it proactively for durable facts the user has just shared
   that should outlive this conversation.
3. **After completing significant work** (a fix, a feature, a decision,
   a deployment), call `observe()` to log what happened so future
   sessions know.
4. **At session start** for unfamiliar contexts, call `list_context(about)`
   to see what is already known about a subject.

Be a thoughtful librarian. Save signal, not noise. The substrate is the
user's vault, not yours.
"""


# ── helpers ─────────────────────────────────────────────────────────────────

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"
RESET = "\033[0m"


@dataclass
class Change:
    """A pending change. Used in both dry-run and apply paths."""

    label: str
    path: Path
    note: str


def _say(prefix: str, msg: str) -> None:
    print(f"  {prefix}  {msg}")


def _ok(msg: str) -> None:
    _say(f"{GREEN}✓{RESET}", msg)


def _skip(msg: str) -> None:
    _say(f"{DIM}·{RESET}", f"{DIM}{msg}{RESET}")


def _warn(msg: str) -> None:
    _say(f"{YELLOW}!{RESET}", msg)


def _err(msg: str) -> None:
    _say(f"{RED}✗{RESET}", msg)


def _backup(path: Path, dry: bool) -> Path | None:
    if not path.exists():
        return None
    backup = path.with_suffix(path.suffix + f".bak.{int(time.time())}")
    if dry:
        return backup
    shutil.copy2(path, backup)
    return backup


def _load_token() -> str:
    token = os.environ.get("TOKEN")
    if token:
        return token
    env_local = Path(".env.local")
    if env_local.exists():
        for line in env_local.read_text().splitlines():
            if line.startswith("NEVERFORGET_AUTH_TOKEN="):
                return line.split("=", 1)[1].strip()
    msg = (
        "could not find token. Set TOKEN env var, or ensure .env.local "
        "has NEVERFORGET_AUTH_TOKEN=..."
    )
    raise SystemExit(msg)


def _append_snippet_if_missing(
    path: Path, *, dry: bool, indent_under_h2: bool = True
) -> Change | None:
    """Append the instruction block to a CLAUDE.md / AGENTS.md / etc.

    Idempotent — checks for SNIPPET_MARKER first.
    """
    if path.exists() and SNIPPET_MARKER in path.read_text():
        return None
    block = f"\n\n{SNIPPET_MARKER}\n\n{SNIPPET_BODY}\n"
    if not indent_under_h2:
        # For .cursorrules (no Markdown headers convention), drop the H2 line
        block = "\n\n# neverforget MCP\n\n" + SNIPPET_BODY + "\n"
    if dry:
        return Change("snippet", path, "would append")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(block)
    return Change("snippet", path, "appended")


# ── Claude Code ─────────────────────────────────────────────────────────────


def install_claude_code(*, token: str, url: str, dry: bool) -> list[Change]:
    # Recent Claude Code reads MCP servers from ~/.claude.json (the user-level
    # config). Older guidance also pointed at ~/.claude/settings.json. We
    # write the MCP entry into both so the server is picked up across
    # versions; the CLAUDE.md snippet only needs one home (the global file).
    primary_path = Path.home() / ".claude.json"
    legacy_path = Path.home() / ".claude" / "settings.json"
    claude_md = Path.home() / ".claude" / "CLAUDE.md"

    if not primary_path.exists() and not legacy_path.exists() and shutil.which("claude") is None:
        _skip("Claude Code not detected (no ~/.claude.json, no settings.json, no `claude` in PATH)")
        return []

    changes: list[Change] = []
    desired = {
        "type": "http",
        "url": url,
        "headers": {"Authorization": f"Bearer {token}"},
    }

    for path, label in [(primary_path, "~/.claude.json"), (legacy_path, "~/.claude/settings.json")]:
        settings: dict[str, Any] = {}
        if path.exists():
            text = path.read_text().strip()
            if text:
                try:
                    settings = json.loads(text)
                except json.JSONDecodeError as e:
                    _err(f"Claude Code {label} is malformed JSON: {e}")
                    continue

        mcp_servers = settings.setdefault("mcpServers", {})
        if mcp_servers.get(SERVER_NAME) == desired:
            _ok(f"Claude Code: {label} already up to date")
            continue

        backup = _backup(path, dry)
        if not dry:
            mcp_servers[SERVER_NAME] = desired
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(settings, indent=2) + "\n")
        action = "would write" if dry else "wrote"
        msg = f"Claude Code: {action} {label}"
        if backup:
            msg += f" (backup: {backup.name})"
        _ok(msg)
        changes.append(Change("settings", path, action))

    snippet_change = _append_snippet_if_missing(claude_md, dry=dry)
    if snippet_change is None:
        _ok(f"Claude Code: {claude_md} already contains the snippet")
    else:
        action = "would append" if dry else "appended"
        _ok(f"Claude Code: {action} snippet to {claude_md}")
        changes.append(snippet_change)

    # Phase 2 #3 — SessionStart lifecycle hook for auto-loaded vault context.
    hook_changes = _install_session_start_hook(token=token, url=url, dry=dry)
    changes.extend(hook_changes)

    return changes


def _install_session_start_hook(*, token: str, url: str, dry: bool) -> list[Change]:
    """Register the scripts/claude_code_hooks/session_start.py hook in
    ``~/.claude/settings.json`` under ``hooks.SessionStart``. The hook
    auto-loads a vault summary into every new Claude Code session so the
    AI starts each session aware of what's in the vault.

    Also writes ``~/.neverforget.env`` with the URL + token so the hook
    can read them without leaking secrets into shell rc files. The env
    file is chmod 600.
    """
    repo_root = Path(__file__).resolve().parent.parent
    hook_script = repo_root / "scripts" / "claude_code_hooks" / "session_start.py"
    settings_path = Path.home() / ".claude" / "settings.json"
    env_path = Path.home() / ".neverforget.env"
    changes: list[Change] = []

    if not hook_script.exists():
        _err(f"Claude Code hook: script not found at {hook_script}")
        return changes

    # 1) write ~/.neverforget.env (gitignored, chmod 600)
    env_content = f"NEVERFORGET_URL={url}\nNEVERFORGET_AUTH_TOKEN={token}\n"
    if not (env_path.exists() and env_path.read_text() == env_content):
        backup = _backup(env_path, dry)
        if not dry:
            env_path.write_text(env_content)
            env_path.chmod(0o600)
        action = "would write" if dry else "wrote"
        msg = f"Claude Code: {action} ~/.neverforget.env (chmod 600)"
        if backup:
            msg += f" (backup: {backup.name})"
        _ok(msg)
        changes.append(Change("env", env_path, action))
    else:
        _ok("Claude Code: ~/.neverforget.env already up to date")

    # 2) register the SessionStart hook
    settings: dict[str, Any] = {}
    if settings_path.exists():
        text = settings_path.read_text().strip()
        if text:
            try:
                settings = json.loads(text)
            except json.JSONDecodeError as e:
                _err(f"Claude Code settings.json malformed: {e}")
                return changes

    hooks = settings.setdefault("hooks", {})
    session_hooks = hooks.setdefault("SessionStart", [])
    hook_command = f"python3 {hook_script}"
    # Idempotency — look for our exact command among existing hooks.
    already = any(
        any(h.get("command") == hook_command for h in entry.get("hooks", []))
        for entry in session_hooks
        if isinstance(entry, dict)
    )
    if already:
        _ok("Claude Code: SessionStart hook already registered")
        return changes

    session_hooks.append(
        {
            "matcher": "",
            "hooks": [{"type": "command", "command": hook_command, "timeout": 10}],
        }
    )
    backup = _backup(settings_path, dry)
    if not dry:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    action = "would register" if dry else "registered"
    msg = f"Claude Code: {action} SessionStart hook → {hook_script}"
    if backup:
        msg += f" (backup: {backup.name})"
    _ok(msg)
    changes.append(Change("hook", settings_path, action))
    return changes


# ── Codex CLI ───────────────────────────────────────────────────────────────


def install_codex(*, token: str, url: str, dry: bool) -> list[Change]:
    config_path = Path.home() / ".codex" / "config.toml"
    agents_md = Path.home() / ".codex" / "AGENTS.md"

    if not config_path.exists() and shutil.which("codex") is None:
        _skip("Codex CLI not detected (no ~/.codex/config.toml, no `codex` in PATH)")
        return []

    changes: list[Change] = []

    existing = config_path.read_text() if config_path.exists() else ""
    # Codex schema (codex-cli 0.133.0): no `type` field, header subtable is
    # `http_headers` (not `headers`). Verified against working sentry and
    # marker-io entries on the host.
    block = (
        f"\n[mcp_servers.{SERVER_NAME}]\n"
        f'url = "{url}"\n\n'
        f"[mcp_servers.{SERVER_NAME}.http_headers]\n"
        f'Authorization = "Bearer {token}"\n'
    )
    marker = f"[mcp_servers.{SERVER_NAME}]"

    if marker in existing:
        _ok(f"Codex: config.toml already has [{marker}] block")
        # Note: we don't surgically replace an existing TOML block on this
        # pass — leave it to the user to remove and re-run if they want a
        # fresh value. Avoids brittle in-place TOML editing.
    else:
        backup = _backup(config_path, dry)
        if not dry:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            with config_path.open("a") as f:
                f.write(block)
        action = "would append" if dry else "appended"
        msg = f"Codex: {action} {marker} block to {config_path}"
        if backup:
            msg += f" (backup: {backup.name})"
        _ok(msg)
        changes.append(Change("config", config_path, action))

    snippet_change = _append_snippet_if_missing(agents_md, dry=dry)
    if snippet_change is None:
        _ok(f"Codex: {agents_md} already contains the snippet")
    else:
        action = "would append" if dry else "appended"
        _ok(f"Codex: {action} snippet to {agents_md}")
        changes.append(snippet_change)

    return changes


# ── Cursor ──────────────────────────────────────────────────────────────────


def install_cursor(*, token: str, url: str, dry: bool) -> list[Change]:
    mcp_path = Path.home() / ".cursor" / "mcp.json"
    rule_path = Path.home() / ".cursor" / "rules" / "neverforget.md"

    cursor_app = Path("/Applications/Cursor.app")
    if not mcp_path.parent.exists() and not cursor_app.exists():
        _skip("Cursor not detected (no ~/.cursor, no /Applications/Cursor.app)")
        return []

    changes: list[Change] = []

    config: dict[str, Any] = {}
    if mcp_path.exists():
        existing_text = mcp_path.read_text().strip()
        if existing_text:
            try:
                config = json.loads(existing_text)
            except json.JSONDecodeError as e:
                _err(f"Cursor mcp.json is malformed: {e}")
                return []

    mcp_servers = config.setdefault("mcpServers", {})
    desired = {
        "type": "http",
        "url": url,
        "headers": {"Authorization": f"Bearer {token}"},
    }

    if mcp_servers.get(SERVER_NAME) == desired:
        _ok("Cursor: mcp.json already up to date")
    else:
        backup = _backup(mcp_path, dry)
        if not dry:
            mcp_servers[SERVER_NAME] = desired
            mcp_path.parent.mkdir(parents=True, exist_ok=True)
            mcp_path.write_text(json.dumps(config, indent=2) + "\n")
        action = "would write" if dry else "wrote"
        msg = f"Cursor: {action} {mcp_path}"
        if backup:
            msg += f" (backup: {backup.name})"
        _ok(msg)
        changes.append(Change("config", mcp_path, action))

    snippet_change = _append_snippet_if_missing(rule_path, dry=dry)
    if snippet_change is None:
        _ok(f"Cursor: {rule_path} already contains the snippet")
    else:
        action = "would write" if dry else "wrote"
        _ok(f"Cursor: {action} snippet at {rule_path}")
        changes.append(snippet_change)

    return changes


# ── Claude.ai ───────────────────────────────────────────────────────────────


def print_claude_ai_instructions(url: str) -> None:
    print()
    print(f"  {YELLOW}!{RESET}  Claude.ai (web/desktop) — UI-only setup, not automatable:")
    print()
    print("       1. Open Claude.ai → Settings → Connectors → Add custom connector")
    print(f"       2. URL:      {url}")
    print("       3. Header:   Authorization: Bearer <your-token>")
    print("       4. Add the instruction snippet to your Custom Instructions")
    print("          (Settings → Profile → Custom Instructions). The snippet is")
    print("          in docs/clients/_snippet.md.")
    print()
    print("       Note: Claude.ai bearer-token MCP has had auth bugs (issue #164).")
    print("       If it fails, Claude Code + Codex CLI both work today.")


# ── main ────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install neverforget MCP server config into detected clients."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing any files.",
    )
    args = parser.parse_args()

    token = _load_token()
    url = os.environ.get("URL", DEFAULT_URL)
    dry = bool(args.dry_run)

    mode = f"{YELLOW}DRY RUN{RESET}" if dry else f"{GREEN}APPLY{RESET}"
    print(f"=== neverforget client installer ({mode}) ===")
    print(f"  url:   {url}")
    print(f"  token: {DIM}from .env.local / TOKEN env (not echoed){RESET}")
    print()

    changes: list[Change] = []
    changes += install_claude_code(token=token, url=url, dry=dry)
    changes += install_codex(token=token, url=url, dry=dry)
    changes += install_cursor(token=token, url=url, dry=dry)
    print_claude_ai_instructions(url)

    print()
    if dry:
        print(f"  {YELLOW}DRY RUN{RESET} — {len(changes)} change(s) would be made.")
        print("  Run without --dry-run to apply.")
    else:
        print(f"  {GREEN}DONE{RESET} — {len(changes)} change(s) applied.")
        print("  Restart any running MCP clients to pick up the new server.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
