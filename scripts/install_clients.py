#!/usr/bin/env python
"""Install the afair MCP server into every detected MCP client.

Detects and configures (only when the client looks installed):

  - Claude Code    → ~/.claude/settings.json + ~/.claude/CLAUDE.md
  - Codex CLI      → ~/.codex/config.toml    + ~/.codex/AGENTS.md
  - Cursor         → ~/.cursor/mcp.json      + ~/.cursor/rules/afair.md
  - GitHub Copilot → VS Code user mcp.json   + prints per-repo snippet step
  - Claude.ai      → UI only; prints manual steps

Idempotent: running again replaces the existing afair entry and does
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

SERVER_NAME = "afair"
# Local self-host default, matching `uv run python -m afair`. Override with
# --url (or the URL env var) for a deployed vault: your Fly app, your own
# domain, or your hosted afair.ai address.
DEFAULT_URL = "http://127.0.0.1:8765/mcp"

# Idempotency markers: the current snippet heading, plus the legacy wrapper
# older installs wrote, so re-running never double-appends.
SNIPPET_MARKERS = (
    "## afair: Persistent Memory Across AI Tools",
    "## afair MCP",
)

# Fallback, used only when docs/clients/_snippet.md can't be read (e.g. the
# script was copied out of the checkout). The live source is that file;
# _load_snippet() keeps the installer and the docs from ever drifting.
_SNIPPET_FALLBACK = """\
## afair: Persistent Memory Across AI Tools

You have access to `afair`, an MCP server that holds my durable memory across
every session, every AI tool, every device. Use it. At conversation start call
`afair.recall(query=...)`. When I share something durable from any part of my
life, work or personal (a decision, a preference, a person, a date that matters,
a plan), call `afair.remember(...)`.
When you take an action (edit a file, run a query, fetch a URL) call
`afair.observe(...)`. Don't ask whether to remember or recall; just do it.
"""


def _load_snippet() -> str:
    """The instruction block, single-sourced from docs/clients/_snippet.md.

    Extracts the first fenced ``markdown`` block so the installer and the docs
    are the same text and can't drift. Falls back to the embedded copy if the
    repo docs aren't reachable.
    """
    doc = Path(__file__).resolve().parent.parent / "docs" / "clients" / "_snippet.md"
    try:
        text = doc.read_text(encoding="utf-8")
    except OSError:
        return _SNIPPET_FALLBACK
    fence = text.find("```markdown")
    if fence == -1:
        return _SNIPPET_FALLBACK
    body_start = text.find("\n", fence) + 1
    body_end = text.find("```", body_start)
    if body_end == -1:
        return _SNIPPET_FALLBACK
    return text[body_start:body_end].strip()


SNIPPET_BODY = _load_snippet()


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
    """The bearer token for a deployed or hosted vault, or "" for a local
    self-host instance (which runs without auth).

    Looked up from the TOKEN env var, then ``.env.local``. Absent is fine: the
    install functions write no Authorization header when the token is empty, so
    a plain ``uv run python -m afair`` + this installer works out of the box.
    """
    token = os.environ.get("TOKEN")
    if token:
        return token
    env_local = Path(".env.local")
    if env_local.exists():
        for line in env_local.read_text().splitlines():
            if line.startswith("AFAIR_AUTH_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


def _append_snippet_if_missing(path: Path, *, dry: bool) -> Change | None:
    """Append the instruction block to a CLAUDE.md / AGENTS.md / rules file.

    Idempotent: skips if any known snippet marker is already present. The block
    is self-headed (it starts with its own H2), so it drops in as-is.
    """
    if path.exists() and any(m in path.read_text() for m in SNIPPET_MARKERS):
        return None
    block = f"\n\n{SNIPPET_BODY}\n"
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
    desired: dict[str, Any] = {"type": "http", "url": url}
    if token:
        desired["headers"] = {"Authorization": f"Bearer {token}"}

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

    # Phase 2 #3: SessionStart lifecycle hook for auto-loaded vault context.
    hook_changes = _install_session_start_hook(token=token, url=url, dry=dry)
    changes.extend(hook_changes)

    return changes


def _install_session_start_hook(*, token: str, url: str, dry: bool) -> list[Change]:
    """Register the scripts/claude_code_hooks/session_start.py hook in
    ``~/.claude/settings.json`` under ``hooks.SessionStart``. The hook
    auto-loads a vault summary into every new Claude Code session so the
    AI starts each session aware of what's in the vault.

    Also writes ``~/.afair.env`` with the URL + token so the hook
    can read them without leaking secrets into shell rc files. The env
    file is chmod 600.
    """
    repo_root = Path(__file__).resolve().parent.parent
    hook_script = repo_root / "scripts" / "claude_code_hooks" / "session_start.py"
    settings_path = Path.home() / ".claude" / "settings.json"
    env_path = Path.home() / ".afair.env"
    changes: list[Change] = []

    if not hook_script.exists():
        _err(f"Claude Code hook: script not found at {hook_script}")
        return changes

    # 1) write ~/.afair.env (gitignored, chmod 600). Local self-host has no
    # token, so omit the line entirely rather than write an empty one.
    env_content = f"AFAIR_URL={url}\n"
    if token:
        env_content += f"AFAIR_AUTH_TOKEN={token}\n"
    if not (env_path.exists() and env_path.read_text() == env_content):
        backup = _backup(env_path, dry)
        if not dry:
            env_path.write_text(env_content)
            env_path.chmod(0o600)
        action = "would write" if dry else "wrote"
        msg = f"Claude Code: {action} ~/.afair.env (chmod 600)"
        if backup:
            msg += f" (backup: {backup.name})"
        _ok(msg)
        changes.append(Change("env", env_path, action))
    else:
        _ok("Claude Code: ~/.afair.env already up to date")

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
    # Idempotency: look for our exact command among existing hooks.
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
    block = f'\n[mcp_servers.{SERVER_NAME}]\nurl = "{url}"\n'
    if token:
        block += f'\n[mcp_servers.{SERVER_NAME}.http_headers]\nAuthorization = "Bearer {token}"\n'
    marker = f"[mcp_servers.{SERVER_NAME}]"

    if marker in existing:
        _ok(f"Codex: config.toml already has [{marker}] block")
        # Note: we don't surgically replace an existing TOML block on this
        # pass: leave it to the user to remove and re-run if they want a
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
    rule_path = Path.home() / ".cursor" / "rules" / "afair.md"

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
    desired: dict[str, Any] = {"type": "http", "url": url}
    if token:
        desired["headers"] = {"Authorization": f"Bearer {token}"}

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


# ── GitHub Copilot (VS Code) ────────────────────────────────────────────────

# macOS app bundle, checked as one of the VS Code detection signals. Module-
# level so tests can point it at a nonexistent path.
VSCODE_APP = Path("/Applications/Visual Studio Code.app")


def _vscode_user_dir() -> Path | None:
    """The VS Code (stable) user-profile dir, per OS, if it exists.

    macOS:   ~/Library/Application Support/Code/User
    Linux:   ~/.config/Code/User
    Windows: %APPDATA%/Code/User

    Returns the first that exists, else None (caller falls back to the
    platform default when VS Code is detected another way).
    """
    home = Path.home()
    candidates = [
        home / "Library" / "Application Support" / "Code" / "User",
        home / ".config" / "Code" / "User",
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "Code" / "User")
    for c in candidates:
        if c.exists():
            return c
    return None


def _vscode_user_dir_default() -> Path:
    """Where VS Code's user mcp.json should live on this OS, even if the
    profile dir doesn't exist yet (VS Code detected via app bundle / PATH)."""
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Code" / "User"
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Code" / "User"
    return home / ".config" / "Code" / "User"


def install_copilot(*, token: str, url: str, dry: bool) -> list[Change]:
    """GitHub Copilot reads MCP servers through VS Code's agent mode from a
    user-level ``mcp.json`` (VS Code 1.102+). Note the format differs from
    Cursor: the top-level key is ``servers`` (not ``mcpServers``), and a remote
    streamable-HTTP server uses ``"type": "http"``.

    Copilot's *instructions* are per-workspace (``.github/copilot-instructions.md``
    or a repo-root ``AGENTS.md``), so there is no global file to write the
    snippet into. We configure the server globally and print the one manual
    step, the same way Claude.ai is handled.
    """
    user_dir = _vscode_user_dir()
    detected = user_dir is not None or VSCODE_APP.exists() or shutil.which("code") is not None
    if not detected:
        _skip("GitHub Copilot / VS Code not detected (no Code user dir, no app, no `code` in PATH)")
        return []
    if user_dir is None:
        user_dir = _vscode_user_dir_default()
    mcp_path = user_dir / "mcp.json"

    changes: list[Change] = []

    config: dict[str, Any] = {}
    if mcp_path.exists():
        existing_text = mcp_path.read_text().strip()
        if existing_text:
            try:
                config = json.loads(existing_text)
            except json.JSONDecodeError as e:
                _err(f"GitHub Copilot: VS Code mcp.json is malformed: {e}")
                return []

    servers = config.setdefault("servers", {})
    desired: dict[str, Any] = {"type": "http", "url": url}
    if token:
        desired["headers"] = {"Authorization": f"Bearer {token}"}

    if servers.get(SERVER_NAME) == desired:
        _ok("GitHub Copilot: VS Code mcp.json already up to date")
    else:
        backup = _backup(mcp_path, dry)
        if not dry:
            servers[SERVER_NAME] = desired
            mcp_path.parent.mkdir(parents=True, exist_ok=True)
            mcp_path.write_text(json.dumps(config, indent=2) + "\n")
        action = "would write" if dry else "wrote"
        msg = f"GitHub Copilot: {action} {mcp_path}"
        if backup:
            msg += f" (backup: {backup.name})"
        _ok(msg)
        changes.append(Change("config", mcp_path, action))

    print()
    _warn("GitHub Copilot: enable agent mode and add the snippet per repo:")
    print("       1. VS Code → Copilot Chat → switch the mode dropdown to Agent.")
    print("       2. The afair tools appear under the tools picker; enable them.")
    print("       3. For autonomous recall/remember, add the instruction snippet to")
    print("          your repo's .github/copilot-instructions.md (or a repo-root")
    print("          AGENTS.md). The snippet is in docs/clients/_snippet.md.")

    return changes


# ── Claude.ai ───────────────────────────────────────────────────────────────


def print_claude_ai_instructions(url: str) -> None:
    print()
    print(f"  {YELLOW}!{RESET}  Claude.ai (web/desktop): UI-only setup, not automatable:")
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
        description="Install afair MCP server config into detected clients."
    )
    parser.add_argument(
        "--url",
        default=None,
        help=f"Your vault's MCP URL. Defaults to the local self-host address "
        f"({DEFAULT_URL}); pass a deployed vault, e.g. https://your-app.fly.dev/mcp.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing any files.",
    )
    args = parser.parse_args()

    token = _load_token()
    url = args.url or os.environ.get("URL") or DEFAULT_URL
    dry = bool(args.dry_run)

    mode = f"{YELLOW}DRY RUN{RESET}" if dry else f"{GREEN}APPLY{RESET}"
    token_status = (
        f"{DIM}from .env.local / TOKEN env (not echoed){RESET}"
        if token
        else f"{DIM}none, local self-host runs without auth{RESET}"
    )
    print(f"=== afair client installer ({mode}) ===")
    print(f"  url:   {url}")
    print(f"  token: {token_status}")
    print()

    changes: list[Change] = []
    changes += install_claude_code(token=token, url=url, dry=dry)
    changes += install_codex(token=token, url=url, dry=dry)
    changes += install_cursor(token=token, url=url, dry=dry)
    changes += install_copilot(token=token, url=url, dry=dry)
    print_claude_ai_instructions(url)

    print()
    if dry:
        print(f"  {YELLOW}DRY RUN{RESET}: {len(changes)} change(s) would be made.")
        print("  Run without --dry-run to apply.")
    else:
        print(f"  {GREEN}DONE{RESET}: {len(changes)} change(s) applied.")
        print("  Restart any running MCP clients to pick up the new server.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
