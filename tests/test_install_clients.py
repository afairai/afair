"""install_clients.py — the one-command installer must work for local self-host.

A local instance (`uv run python -m afair`) runs without auth, so the installer
has to produce client configs with no Authorization header and the local
default URL. With a token (deployed / hosted vault) it writes the header. These
guard the self-host hero path that the README advertises.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "install_clients.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("install_clients", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so the module's @dataclass can resolve its __module__.
    sys.modules["install_clients"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def installer() -> ModuleType:
    return _load_module()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _claude_entry(home: Path) -> dict[str, Any]:
    cfg = json.loads((home / ".claude.json").read_text())
    return cfg["mcpServers"]["afair"]


# ── Claude Code (JSON config) ────────────────────────────────────────────────


def test_claude_code_local_no_token_omits_auth(installer: ModuleType, home: Path) -> None:
    (home / ".claude.json").write_text("{}")  # so the client is "detected"
    installer.install_claude_code(token="", url="http://127.0.0.1:8765/mcp", dry=False)
    entry = _claude_entry(home)
    assert entry == {"type": "http", "url": "http://127.0.0.1:8765/mcp"}
    assert "headers" not in entry
    # the session-start hook env also omits the token line
    assert "AFAIR_AUTH_TOKEN" not in (home / ".afair.env").read_text()


def test_claude_code_with_token_includes_auth(installer: ModuleType, home: Path) -> None:
    (home / ".claude.json").write_text("{}")
    installer.install_claude_code(token="tok123", url="https://x.fly.dev/mcp", dry=False)
    entry = _claude_entry(home)
    assert entry["url"] == "https://x.fly.dev/mcp"
    assert entry["headers"] == {"Authorization": "Bearer tok123"}


# ── Codex (TOML config) ──────────────────────────────────────────────────────


def test_codex_local_no_token_omits_http_headers(installer: ModuleType, home: Path) -> None:
    cfg = home / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("")
    installer.install_codex(token="", url="http://127.0.0.1:8765/mcp", dry=False)
    text = cfg.read_text()
    assert "[mcp_servers.afair]" in text
    assert 'url = "http://127.0.0.1:8765/mcp"' in text
    assert "http_headers" not in text
    assert "Authorization" not in text


def test_codex_with_token_includes_http_headers(installer: ModuleType, home: Path) -> None:
    cfg = home / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("")
    installer.install_codex(token="tok123", url="https://x.fly.dev/mcp", dry=False)
    text = cfg.read_text()
    assert "[mcp_servers.afair.http_headers]" in text
    assert 'Authorization = "Bearer tok123"' in text


# ── GitHub Copilot (VS Code mcp.json) ────────────────────────────────────────


def _vscode_mcp(home: Path) -> Path:
    """The macOS VS Code user dir under the (fake) HOME. Creating it makes the
    installer treat VS Code as detected regardless of the real OS."""
    return home / "Library" / "Application Support" / "Code" / "User" / "mcp.json"


def test_copilot_local_no_token_uses_servers_key_and_omits_auth(
    installer: ModuleType, home: Path
) -> None:
    mcp = _vscode_mcp(home)
    mcp.parent.mkdir(parents=True)  # so VS Code is "detected"
    installer.install_copilot(token="", url="http://127.0.0.1:8765/mcp", dry=False)
    cfg = json.loads(mcp.read_text())
    # VS Code uses the top-level "servers" key, NOT "mcpServers" like Cursor.
    assert "mcpServers" not in cfg
    entry = cfg["servers"]["afair"]
    assert entry == {"type": "http", "url": "http://127.0.0.1:8765/mcp"}
    assert "headers" not in entry


def test_copilot_with_token_includes_auth(installer: ModuleType, home: Path) -> None:
    mcp = _vscode_mcp(home)
    mcp.parent.mkdir(parents=True)
    installer.install_copilot(token="tok123", url="https://x.fly.dev/mcp", dry=False)
    entry = json.loads(mcp.read_text())["servers"]["afair"]
    assert entry["url"] == "https://x.fly.dev/mcp"
    assert entry["headers"] == {"Authorization": "Bearer tok123"}


def test_copilot_preserves_existing_servers(installer: ModuleType, home: Path) -> None:
    mcp = _vscode_mcp(home)
    mcp.parent.mkdir(parents=True)
    mcp.write_text(json.dumps({"servers": {"other": {"type": "http", "url": "http://x/mcp"}}}))
    installer.install_copilot(token="", url="http://127.0.0.1:8765/mcp", dry=False)
    servers = json.loads(mcp.read_text())["servers"]
    assert set(servers) == {"other", "afair"}  # existing entry untouched


def test_copilot_skips_when_vscode_absent(
    installer: ModuleType, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No VS Code user dir (none created), no app bundle, no `code` on PATH.
    monkeypatch.setattr(installer.shutil, "which", lambda _: None)
    monkeypatch.setattr(installer, "VSCODE_APP", home / "nope" / "VSCode.app")
    changes = installer.install_copilot(token="", url="http://127.0.0.1:8765/mcp", dry=False)
    assert changes == []
    assert not _vscode_mcp(home).exists()


# ── token lookup ─────────────────────────────────────────────────────────────


def test_load_token_returns_empty_when_absent(
    installer: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env.local in this cwd
    assert installer._load_token() == ""
