#!/usr/bin/env python
"""Install the afair MCP server into every detected MCP client.

Detects and configures (only when the client looks installed):

  - Claude Code        → ~/.claude/settings.json + ~/.claude/CLAUDE.md
  - Codex CLI          → ~/.codex/config.toml    + ~/.codex/AGENTS.md
  - Cursor             → ~/.cursor/mcp.json      + ~/.cursor/rules/afair.md
  - GitHub Copilot     → VS Code user mcp.json   + prints per-repo snippet step
  - GitHub Copilot CLI → ~/.copilot/mcp-config.json          + snippet note
  - Gemini CLI         → ~/.gemini/settings.json             + snippet note
  - Windsurf           → ~/.codeium/windsurf/mcp_config.json + snippet note
  - Antigravity        → ~/.gemini/config/mcp_config.json    + snippet note
  - Claude.ai          → UI only; prints manual steps

Idempotent and safe to re-run. Without an explicit --url it keeps whatever
URL a client already points at (so refreshing the snippet never repoints your
deployed vault); an outdated snippet is left alone unless you confirm (on a
TTY) or pass --update-snippet. Always backs up any file it changes to
``<path>.bak.<timestamp>`` so revert is one ``mv`` away.

On a terminal it shows an interactive picker so it never lands everywhere by
surprise. A piped / non-TTY run (e.g. `yes | ...`, CI) keeps the old "all
detected" behaviour. --only / --skip choose non-interactively; --yes forces all.

Usage:
    uv run python scripts/install_clients.py                  # interactive picker
    uv run python scripts/install_clients.py --yes            # all detected, no prompt
    uv run python scripts/install_clients.py --dry-run        # preview only
    uv run python scripts/install_clients.py --list           # show client keys
    uv run python scripts/install_clients.py --only copilot   # just one
    uv run python scripts/install_clients.py --only claude-code,codex
    uv run python scripts/install_clients.py --skip cursor    # all but one
    uv run python scripts/install_clients.py --update-snippet # refresh the prompt
    URL=https://your-vault/mcp uv run python scripts/install_clients.py  # set/keep URL
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
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from collections.abc import Callable

# ── config ──────────────────────────────────────────────────────────────────

SERVER_NAME = "afair"
# Local self-host default, matching `uv run python -m afair`. Override with
# --url (or the URL env var) for a deployed vault: your Fly app, your own
# domain, or your hosted afair.ai address.
DEFAULT_URL = "http://127.0.0.1:8765/mcp"

# Selectable client keys, in install order. `claude-ai` is print-only (its setup
# is a UI walk-through, not a file write). Used by --only / --skip.
CLIENT_KEYS: tuple[str, ...] = (
    "claude-code",
    "codex",
    "cursor",
    "copilot",
    "copilot-cli",
    "gemini-cli",
    "windsurf",
    "antigravity",
    "claude-ai",
)

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
When a recall result shows `pending_corrections_count` > 0, tell me once per
session that I have memories to review, then offer `recall(stats=True)`.
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


def _read_json_url(path: Path, top_key: str, url_field: str = "url") -> str | None:
    """The existing afair server URL in a JSON config (or None). ``url_field``
    varies per client: 'url' (Claude/Cursor/VS Code/Copilot CLI), 'httpUrl'
    (Gemini CLI), 'serverUrl' (Windsurf/Antigravity)."""
    if not path.exists():
        return None
    try:
        cfg = json.loads(path.read_text() or "{}")
    except json.JSONDecodeError:
        return None
    entry = cfg.get(top_key, {}).get(SERVER_NAME)
    return entry.get(url_field) if isinstance(entry, dict) else None


def _pick_url(existing_url: str | None, url: str, *, url_explicit: bool, label: str) -> str:
    """The URL to write for a client. An explicit --url/URL always wins. Without
    one, an existing non-matching URL is kept (and a warning printed) instead of
    being reset to the localhost default, so re-running the installer just to
    refresh the snippet never silently repoints your deployed vault."""
    if not url_explicit and existing_url and existing_url != url:
        _warn(f"{label}: keeping existing URL {existing_url} (pass --url to change it)")
        return existing_url
    return url


def _replace_snippet_block(text: str) -> str:
    """Swap an existing afair snippet section for the current SNIPPET_BODY. The
    snippet is one self-headed H2; the block runs from its heading to the next
    sibling H2 (``\\n## ``) or end of file."""
    for marker in SNIPPET_MARKERS:
        idx = text.find(marker)
        if idx == -1:
            continue
        line_start = text.rfind("\n", 0, idx)
        start = 0 if line_start == -1 else line_start + 1
        nxt = text.find("\n## ", idx + len(marker))
        end = len(text) if nxt == -1 else nxt + 1
        return text[:start] + SNIPPET_BODY + "\n" + text[end:]
    return text


def _ensure_snippet(
    path: Path,
    *,
    dry: bool,
    update: str = "no",
    input_fn: Callable[[str], str] = input,
) -> Change | None:
    """Keep the instruction snippet in a CLAUDE.md / AGENTS.md / rules file
    present and current. Prints its own status; returns a Change for accounting.

    - missing            -> append the current block.
    - already current    -> no-op.
    - present, outdated  -> refresh, gated by `update`: 'yes' rewrites, 'ask'
      prompts (interactive), 'no' warns and leaves it, so a plain re-run never
      rewrites the prompt without consent.
    """
    text = path.read_text() if path.exists() else ""
    present = bool(text) and any(m in text for m in SNIPPET_MARKERS)

    if not present:
        if dry:
            _ok(f"snippet: would append to {path}")
            return Change("snippet", path, "would append")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(f"\n\n{SNIPPET_BODY}\n")
        _ok(f"snippet: appended to {path}")
        return Change("snippet", path, "appended")

    if SNIPPET_BODY.strip() in text:
        _ok(f"snippet: already current in {path}")
        return None

    # present but outdated
    if update == "no":
        _warn(
            f"snippet: {path} is outdated (pass --update-snippet or run interactively to refresh)"
        )
        return None
    if update == "ask":
        ans = input_fn(f"  afair snippet in {path} is outdated. Update it? [y/N]: ").strip().lower()
        if ans not in {"y", "yes"}:
            _skip(f"snippet: left {path} unchanged")
            return None
    if dry:
        _ok(f"snippet: would update {path}")
        return Change("snippet", path, "would update")
    _backup(path, dry)
    path.write_text(_replace_snippet_block(text))
    _ok(f"snippet: updated {path}")
    return Change("snippet", path, "updated")


# ── Claude Code ─────────────────────────────────────────────────────────────


def install_claude_code(
    *, token: str, url: str, dry: bool, url_explicit: bool = False, snippet_update: str = "no"
) -> list[Change]:
    # Recent Claude Code reads MCP servers from ~/.claude.json (the user-level
    # config). Older guidance also pointed at ~/.claude/settings.json. We
    # write the MCP entry into both so the server is picked up across
    # versions; the CLAUDE.md snippet only needs one home (the global file).
    primary_path = Path.home() / ".claude.json"
    legacy_path = Path.home() / ".claude" / "settings.json"
    claude_md = Path.home() / ".claude" / "CLAUDE.md"

    if not _detect_claude_code():
        _skip("Claude Code not detected (no ~/.claude.json, no settings.json, no `claude` in PATH)")
        return []

    existing_url = _read_json_url(primary_path, "mcpServers")
    url = _pick_url(existing_url, url, url_explicit=url_explicit, label="Claude Code")

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

    snippet_change = _ensure_snippet(claude_md, dry=dry, update=snippet_update)
    if snippet_change is not None:
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


def install_codex(
    *, token: str, url: str, dry: bool, url_explicit: bool = False, snippet_update: str = "no"
) -> list[Change]:
    config_path = Path.home() / ".codex" / "config.toml"
    agents_md = Path.home() / ".codex" / "AGENTS.md"

    if not _detect_codex():
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
        # We don't surgically rewrite an existing TOML block (avoids brittle
        # in-place TOML editing), which also means Codex's URL is never
        # clobbered on a re-run. To change it, remove the block and re-run.
        _ok(f"Codex: config.toml already has [{marker}] block (url unchanged)")
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

    snippet_change = _ensure_snippet(agents_md, dry=dry, update=snippet_update)
    if snippet_change is not None:
        changes.append(snippet_change)

    return changes


# ── Cursor ──────────────────────────────────────────────────────────────────


def install_cursor(
    *, token: str, url: str, dry: bool, url_explicit: bool = False, snippet_update: str = "no"
) -> list[Change]:
    mcp_path = Path.home() / ".cursor" / "mcp.json"
    rule_path = Path.home() / ".cursor" / "rules" / "afair.md"

    if not _detect_cursor():
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
    existing = mcp_servers.get(SERVER_NAME)
    url = _pick_url(
        existing.get("url") if isinstance(existing, dict) else None,
        url,
        url_explicit=url_explicit,
        label="Cursor",
    )
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

    snippet_change = _ensure_snippet(rule_path, dry=dry, update=snippet_update)
    if snippet_change is not None:
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


def install_copilot(
    *, token: str, url: str, dry: bool, url_explicit: bool = False, snippet_update: str = "no"
) -> list[Change]:
    """GitHub Copilot reads MCP servers through VS Code's agent mode from a
    user-level ``mcp.json`` (VS Code 1.102+). Note the format differs from
    Cursor: the top-level key is ``servers`` (not ``mcpServers``), and a remote
    streamable-HTTP server uses ``"type": "http"``.

    Copilot's *instructions* are per-workspace (``.github/copilot-instructions.md``
    or a repo-root ``AGENTS.md``), so there is no global file to write the
    snippet into. We configure the server globally and print the one manual
    step, the same way Claude.ai is handled.
    """
    if not _detect_copilot():
        _skip("GitHub Copilot / VS Code not detected (no Code user dir, no app, no `code` in PATH)")
        return []
    user_dir = _vscode_user_dir()
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
    existing = servers.get(SERVER_NAME)
    url = _pick_url(
        existing.get("url") if isinstance(existing, dict) else None,
        url,
        url_explicit=url_explicit,
        label="GitHub Copilot",
    )
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


# ── other terminal MCP clients (shared "mcpServers" JSON shape) ──────────────
#
# These all store remote MCP servers in a JSON file with a top-level
# "mcpServers" object, but each names the URL field differently (verified
# against each tool's docs, 2026-07):
#   - GitHub Copilot CLI  ~/.copilot/mcp-config.json           type:http + url + tools
#   - Gemini CLI          ~/.gemini/settings.json              httpUrl
#   - Windsurf            ~/.codeium/windsurf/mcp_config.json  serverUrl
#   - Antigravity         ~/.gemini/config/mcp_config.json     serverUrl
# Their per-tool instruction files differ and are easy to get wrong, so we write
# the server wiring (the part that matters) and print one snippet pointer.

WINDSURF_APP = Path("/Applications/Windsurf.app")
ANTIGRAVITY_APP = Path("/Applications/Antigravity.app")


def _write_mcpservers_entry(
    *,
    label: str,
    path: Path,
    entry: dict[str, Any],
    url_field: str,
    url_explicit: bool,
    dry: bool,
) -> list[Change]:
    """Merge the afair entry into a ``{"mcpServers": {...}}`` JSON file.

    Shared by the clients that use that schema; only the per-client ``entry``
    shape (url vs httpUrl vs serverUrl, etc.) differs. Existing servers are
    preserved, the file is backed up, and a matching entry is a no-op. Without
    an explicit --url, an existing non-matching URL under ``url_field`` is kept
    rather than reset to the default."""
    config: dict[str, Any] = {}
    if path.exists():
        text = path.read_text().strip()
        if text:
            try:
                config = json.loads(text)
            except json.JSONDecodeError as e:
                _err(f"{label}: {path} is malformed: {e}")
                return []
    servers = config.setdefault("mcpServers", {})
    existing = servers.get(SERVER_NAME)
    entry[url_field] = _pick_url(
        existing.get(url_field) if isinstance(existing, dict) else None,
        entry[url_field],
        url_explicit=url_explicit,
        label=label,
    )
    if servers.get(SERVER_NAME) == entry:
        _ok(f"{label}: {path.name} already up to date")
        return []
    backup = _backup(path, dry)
    if not dry:
        servers[SERVER_NAME] = entry
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2) + "\n")
    action = "would write" if dry else "wrote"
    msg = f"{label}: {action} {path}"
    if backup:
        msg += f" (backup: {backup.name})"
    _ok(msg)
    return [Change("config", path, action)]


def _snippet_note(label: str, where: str) -> None:
    _warn(f"{label}: add the instruction snippet to {where}")
    print("       (the snippet is in docs/clients/_snippet.md), so the agent")
    print("       reaches for recall/remember/observe on its own.")


def _detect_copilot_cli() -> bool:
    return (Path.home() / ".copilot").exists() or shutil.which("copilot") is not None


def _detect_gemini_cli() -> bool:
    return (Path.home() / ".gemini" / "settings.json").exists() or shutil.which(
        "gemini"
    ) is not None


def _detect_windsurf() -> bool:
    return (Path.home() / ".codeium" / "windsurf").exists() or WINDSURF_APP.exists()


def _detect_antigravity() -> bool:
    return (Path.home() / ".gemini" / "config").exists() or ANTIGRAVITY_APP.exists()


def install_copilot_cli(
    *, token: str, url: str, dry: bool, url_explicit: bool = False, snippet_update: str = "no"
) -> list[Change]:
    if not _detect_copilot_cli():
        _skip("GitHub Copilot CLI not detected (no ~/.copilot, no `copilot` in PATH)")
        return []
    path = Path.home() / ".copilot" / "mcp-config.json"
    entry: dict[str, Any] = {"type": "http", "url": url, "tools": ["*"]}
    if token:
        entry["headers"] = {"Authorization": f"Bearer {token}"}
    changes = _write_mcpservers_entry(
        label="GitHub Copilot CLI",
        path=path,
        entry=entry,
        url_field="url",
        url_explicit=url_explicit,
        dry=dry,
    )
    _snippet_note("GitHub Copilot CLI", "a repo .github/copilot-instructions.md or AGENTS.md")
    return changes


def install_gemini_cli(
    *, token: str, url: str, dry: bool, url_explicit: bool = False, snippet_update: str = "no"
) -> list[Change]:
    if not _detect_gemini_cli():
        _skip("Gemini CLI not detected (no ~/.gemini/settings.json, no `gemini` in PATH)")
        return []
    path = Path.home() / ".gemini" / "settings.json"
    # Gemini CLI uses `httpUrl` (not `url`) for the Streamable HTTP transport.
    entry: dict[str, Any] = {"httpUrl": url}
    if token:
        entry["headers"] = {"Authorization": f"Bearer {token}"}
    changes = _write_mcpservers_entry(
        label="Gemini CLI",
        path=path,
        entry=entry,
        url_field="httpUrl",
        url_explicit=url_explicit,
        dry=dry,
    )
    _snippet_note("Gemini CLI", "~/.gemini/GEMINI.md")
    return changes


def install_windsurf(
    *, token: str, url: str, dry: bool, url_explicit: bool = False, snippet_update: str = "no"
) -> list[Change]:
    if not _detect_windsurf():
        _skip("Windsurf not detected (no ~/.codeium/windsurf, no /Applications/Windsurf.app)")
        return []
    path = Path.home() / ".codeium" / "windsurf" / "mcp_config.json"
    # Windsurf uses `serverUrl` (not `url`) for remote HTTP servers.
    entry: dict[str, Any] = {"serverUrl": url}
    if token:
        entry["headers"] = {"Authorization": f"Bearer {token}"}
    changes = _write_mcpservers_entry(
        label="Windsurf",
        path=path,
        entry=entry,
        url_field="serverUrl",
        url_explicit=url_explicit,
        dry=dry,
    )
    _snippet_note("Windsurf", "Windsurf → Settings → Rules (global rules)")
    return changes


def install_antigravity(
    *, token: str, url: str, dry: bool, url_explicit: bool = False, snippet_update: str = "no"
) -> list[Change]:
    if not _detect_antigravity():
        _skip("Antigravity not detected (no ~/.gemini/config, no /Applications/Antigravity.app)")
        return []
    path = Path.home() / ".gemini" / "config" / "mcp_config.json"
    # Antigravity also uses `serverUrl` for remote HTTP servers.
    entry: dict[str, Any] = {"serverUrl": url}
    if token:
        entry["headers"] = {"Authorization": f"Bearer {token}"}
    changes = _write_mcpservers_entry(
        label="Antigravity",
        path=path,
        entry=entry,
        url_field="serverUrl",
        url_explicit=url_explicit,
        dry=dry,
    )
    _snippet_note("Antigravity", "a repo-root AGENTS.md")
    return changes


# ── Claude.ai ───────────────────────────────────────────────────────────────


def print_claude_ai_instructions(url: str) -> None:
    print()
    if _is_loopback(url):
        # A cloud web client cannot reach a loopback address. Telling the user
        # to paste their localhost into Claude.ai would just fail to connect.
        print(f"  {YELLOW}!{RESET}  Claude.ai (web): can't use your local server.")
        print()
        print(f"       Your vault is at {url}, a localhost address. Claude.ai")
        print("       runs in Anthropic's cloud and cannot reach your machine, so")
        print("       there is nothing to paste. To use web clients, deploy afair")
        print("       to a public HTTPS URL (see docs/self-hosting.md), then re-run")
        print("       this with --url https://your-vault/mcp --only claude-ai.")
        print("       For local use, the CLI/desktop clients above are the way.")
        return
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


# ── client selection ─────────────────────────────────────────────────────────


def select_clients(only: str | None, skip: str | None) -> list[str]:
    """Resolve --only / --skip into the ordered list of client keys to run.

    Default (neither given) is every key, the historical behaviour. --only and
    --skip are mutually exclusive. Unknown names raise ValueError listing the
    valid keys, so a typo fails loudly instead of silently installing nothing.
    """
    if only and skip:
        raise ValueError("pass either --only or --skip, not both")
    valid = set(CLIENT_KEYS)

    def _parse(raw: str) -> set[str]:
        names = {n.strip().lower() for n in raw.split(",") if n.strip()}
        unknown = names - valid
        if unknown:
            raise ValueError(
                f"unknown client(s): {', '.join(sorted(unknown))}. valid: {', '.join(CLIENT_KEYS)}"
            )
        return names

    if only:
        chosen = _parse(only)
    elif skip:
        chosen = valid - _parse(skip)
    else:
        chosen = valid
    return [k for k in CLIENT_KEYS if k in chosen]


# ── detection + interactive picker ───────────────────────────────────────────

CLIENT_LABELS: dict[str, str] = {
    "claude-code": "Claude Code",
    "codex": "Codex CLI",
    "cursor": "Cursor",
    "copilot": "GitHub Copilot (VS Code)",
    "copilot-cli": "GitHub Copilot CLI",
    "gemini-cli": "Gemini CLI",
    "windsurf": "Windsurf",
    "antigravity": "Antigravity",
    "claude-ai": "Claude.ai (web)",
}


def _is_loopback(url: str) -> bool:
    """True when the MCP URL points at this machine. A web client (Claude.ai,
    ChatGPT) runs in the vendor's cloud and cannot reach a loopback address, so
    those clients are only offered against a public URL."""
    host = (urlparse(url).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _detect_claude_code() -> bool:
    return (
        (Path.home() / ".claude.json").exists()
        or (Path.home() / ".claude" / "settings.json").exists()
        or shutil.which("claude") is not None
    )


def _detect_codex() -> bool:
    return (Path.home() / ".codex" / "config.toml").exists() or shutil.which("codex") is not None


def _detect_cursor() -> bool:
    return (Path.home() / ".cursor").exists() or Path("/Applications/Cursor.app").exists()


def _detect_copilot() -> bool:
    return _vscode_user_dir() is not None or VSCODE_APP.exists() or shutil.which("code") is not None


def detect_clients(url: str) -> dict[str, bool]:
    """Which selectable clients are usable right now. The CLI/desktop clients
    are "detected" if installed; Claude.ai is a web client, so it's usable only
    when the URL is public (it can't reach your localhost)."""
    return {
        "claude-code": _detect_claude_code(),
        "codex": _detect_codex(),
        "cursor": _detect_cursor(),
        "copilot": _detect_copilot(),
        "copilot-cli": _detect_copilot_cli(),
        "gemini-cli": _detect_gemini_cli(),
        "windsurf": _detect_windsurf(),
        "antigravity": _detect_antigravity(),
        "claude-ai": not _is_loopback(url),
    }


def prompt_clients(
    detected: dict[str, bool], *, input_fn: Callable[[str], str] = input
) -> list[str]:
    """Interactive picker. Lists every client with its status; the default
    (empty input or 'a') selects the usable ones. Accepts numbers, names, or
    'q' to cancel. Returns the chosen keys in CLIENT_KEYS order ([] = cancel)."""
    keys = list(CLIENT_KEYS)
    default = [k for k in keys if detected.get(k)]
    print("Which clients should afair install into?")
    for i, key in enumerate(keys, 1):
        if detected.get(key):
            tag = f"{GREEN}available{RESET}"
        elif key == "claude-ai":
            tag = f"{DIM}needs a public URL (can't reach localhost){RESET}"
        else:
            tag = f"{DIM}not found{RESET}"
        print(f"  {i}) {CLIENT_LABELS[key]:<26} {tag}")
    print()
    hint = "Numbers (e.g. 1,2), names, 'a' = all available, 'q' = cancel [a]: "
    while True:
        raw = input_fn(hint).strip().lower()
        if raw in {"q", "quit"}:
            return []
        if raw in {"", "a", "all"}:
            return default
        chosen: set[str] = set()
        ok = True
        for tok in raw.replace(",", " ").split():
            if tok.isdigit() and 1 <= int(tok) <= len(keys):
                chosen.add(keys[int(tok) - 1])
            elif tok in CLIENT_KEYS:
                chosen.add(tok)
            else:
                ok = False
        if ok and chosen:
            return [k for k in keys if k in chosen]
        _warn("Didn't understand that. Use numbers, names, 'a', or 'q'.")


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
    parser.add_argument(
        "--only",
        default=None,
        metavar="CLIENT[,CLIENT...]",
        help=f"Install into only these clients (comma-separated). "
        f"Valid: {', '.join(CLIENT_KEYS)}. Mutually exclusive with --skip.",
    )
    parser.add_argument(
        "--skip",
        default=None,
        metavar="CLIENT[,CLIENT...]",
        help="Install into every detected client except these (comma-separated). "
        "Mutually exclusive with --only.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List the selectable client keys and exit.",
    )
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="Force the interactive picker (default on a terminal when no "
        "--only/--skip/--yes is given).",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Don't prompt; install into all detected clients (the old behaviour).",
    )
    parser.add_argument(
        "--update-snippet",
        action="store_true",
        help="Refresh an outdated instruction snippet without prompting. By "
        "default an outdated snippet is left alone (or you're asked, on a TTY).",
    )
    args = parser.parse_args()

    if args.list:
        print("Selectable clients (use with --only / --skip):")
        for key in CLIENT_KEYS:
            print(f"  {key}")
        return 0

    token = _load_token()
    url = args.url or os.environ.get("URL") or DEFAULT_URL
    # Whether the URL was chosen explicitly (vs the localhost default). Without
    # an explicit URL, installers keep whatever URL a client already has, so
    # re-running just to refresh the snippet never repoints a deployed vault.
    url_explicit = bool(args.url or os.environ.get("URL"))
    # Snippet refresh policy for an already-present-but-outdated snippet.
    if args.update_snippet:
        snippet_update = "yes"
    elif sys.stdin.isatty():
        snippet_update = "ask"
    else:
        snippet_update = "no"
    dry = bool(args.dry_run)

    # Choose targets. Explicit --only/--skip wins. Otherwise prompt on a TTY
    # (unless --yes), so it never lands everywhere by surprise; a piped/CI run
    # (no TTY) keeps the old "all detected" behaviour so `yes | ...` still works.
    try:
        if args.only or args.skip:
            selected = select_clients(args.only, args.skip)
        elif args.interactive or (sys.stdin.isatty() and not args.yes):
            selected = prompt_clients(detect_clients(url))
            if not selected:
                _warn("Cancelled, nothing installed.")
                return 0
        else:
            selected = list(CLIENT_KEYS)
    except ValueError as e:
        _err(str(e))
        return 2
    if not selected:
        _warn("No clients selected, nothing to do.")
        return 0

    mode = f"{YELLOW}DRY RUN{RESET}" if dry else f"{GREEN}APPLY{RESET}"
    token_status = (
        f"{DIM}from .env.local / TOKEN env (not echoed){RESET}"
        if token
        else f"{DIM}none, local self-host runs without auth{RESET}"
    )
    print(f"=== afair client installer ({mode}) ===")
    print(f"  url:     {url}")
    print(f"  token:   {token_status}")
    print(f"  targets: {', '.join(selected)}")
    print()

    # Keyed dispatch so --only / --skip select exactly which run. Order follows
    # CLIENT_KEYS via `selected`. claude-ai is print-only (UI setup).
    file_installers = {
        "claude-code": install_claude_code,
        "codex": install_codex,
        "cursor": install_cursor,
        "copilot": install_copilot,
        "copilot-cli": install_copilot_cli,
        "gemini-cli": install_gemini_cli,
        "windsurf": install_windsurf,
        "antigravity": install_antigravity,
    }
    changes: list[Change] = []
    for key in selected:
        if key == "claude-ai":
            print_claude_ai_instructions(url)
        else:
            changes += file_installers[key](
                token=token,
                url=url,
                dry=dry,
                url_explicit=url_explicit,
                snippet_update=snippet_update,
            )

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
