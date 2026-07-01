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


# ── client selection (--only / --skip) ───────────────────────────────────────


def test_select_clients_default_is_all(installer: ModuleType) -> None:
    assert installer.select_clients(None, None) == list(installer.CLIENT_KEYS)


def test_select_clients_only_filters_and_keeps_order(installer: ModuleType) -> None:
    # Order follows CLIENT_KEYS, not the order the user typed.
    assert installer.select_clients("codex,claude-code", None) == ["claude-code", "codex"]


def test_select_clients_skip_excludes(installer: ModuleType) -> None:
    out = installer.select_clients(None, "cursor,claude-ai")
    assert "cursor" not in out and "claude-ai" not in out
    assert "claude-code" in out and "copilot" in out


def test_select_clients_is_case_and_space_insensitive(installer: ModuleType) -> None:
    assert installer.select_clients(" Copilot , CURSOR ", None) == ["cursor", "copilot"]


def test_select_clients_unknown_name_raises(installer: ModuleType) -> None:
    with pytest.raises(ValueError, match="unknown client"):
        installer.select_clients("vscode", None)


def test_select_clients_only_and_skip_conflict(installer: ModuleType) -> None:
    with pytest.raises(ValueError, match="either --only or --skip"):
        installer.select_clients("codex", "cursor")


# ── other terminal clients (Copilot CLI / Gemini CLI / Windsurf / Antigravity) ─


def test_copilot_cli_uses_type_http_url_and_tools(installer: ModuleType, home: Path) -> None:
    (home / ".copilot").mkdir()  # detected
    installer.install_copilot_cli(token="", url="http://127.0.0.1:8765/mcp", dry=False)
    entry = json.loads((home / ".copilot" / "mcp-config.json").read_text())["mcpServers"]["afair"]
    assert entry == {"type": "http", "url": "http://127.0.0.1:8765/mcp", "tools": ["*"]}


def test_gemini_cli_uses_httpurl_field(installer: ModuleType, home: Path) -> None:
    (home / ".gemini").mkdir()
    (home / ".gemini" / "settings.json").write_text("{}")  # detected
    installer.install_gemini_cli(token="tok", url="https://v.fly.dev/mcp", dry=False)
    entry = json.loads((home / ".gemini" / "settings.json").read_text())["mcpServers"]["afair"]
    # Gemini CLI's streamable-HTTP field is httpUrl, not url.
    assert entry == {"httpUrl": "https://v.fly.dev/mcp", "headers": {"Authorization": "Bearer tok"}}


def test_windsurf_uses_serverurl_field(installer: ModuleType, home: Path) -> None:
    (home / ".codeium" / "windsurf").mkdir(parents=True)  # detected
    installer.install_windsurf(token="", url="http://127.0.0.1:8765/mcp", dry=False)
    cfg = json.loads((home / ".codeium" / "windsurf" / "mcp_config.json").read_text())
    assert cfg["mcpServers"]["afair"] == {"serverUrl": "http://127.0.0.1:8765/mcp"}


def test_antigravity_uses_serverurl_field(installer: ModuleType, home: Path) -> None:
    (home / ".gemini" / "config").mkdir(parents=True)  # detected
    installer.install_antigravity(token="", url="http://127.0.0.1:8765/mcp", dry=False)
    cfg = json.loads((home / ".gemini" / "config" / "mcp_config.json").read_text())
    assert cfg["mcpServers"]["afair"] == {"serverUrl": "http://127.0.0.1:8765/mcp"}


def test_new_clients_skip_when_absent(
    installer: ModuleType, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(installer.shutil, "which", lambda _: None)
    monkeypatch.setattr(installer, "WINDSURF_APP", home / "nope1")
    monkeypatch.setattr(installer, "ANTIGRAVITY_APP", home / "nope2")
    for fn in (
        installer.install_copilot_cli,
        installer.install_gemini_cli,
        installer.install_windsurf,
        installer.install_antigravity,
    ):
        assert fn(token="", url="http://127.0.0.1:8765/mcp", dry=False) == []


# ── loopback / web-client gating ─────────────────────────────────────────────


def test_is_loopback(installer: ModuleType) -> None:
    assert installer._is_loopback("http://127.0.0.1:8765/mcp")
    assert installer._is_loopback("http://localhost:8765/mcp")
    assert not installer._is_loopback("https://my-vault.fly.dev/mcp")


def test_detect_clients_gates_claude_ai_on_public_url(installer: ModuleType, home: Path) -> None:
    # Claude.ai is a cloud web client: usable only against a public URL.
    assert installer.detect_clients("http://127.0.0.1:8765/mcp")["claude-ai"] is False
    assert installer.detect_clients("https://vault.example.com/mcp")["claude-ai"] is True


# ── interactive picker ───────────────────────────────────────────────────────


def _detected_all_but_ai() -> dict[str, bool]:
    return {
        "claude-code": True,
        "codex": True,
        "cursor": False,
        "copilot": True,
        "claude-ai": False,
    }


def test_prompt_empty_input_selects_available(installer: ModuleType) -> None:
    # Enter / 'a' picks the detected (available) ones, skipping not-founds.
    out = installer.prompt_clients(_detected_all_but_ai(), input_fn=lambda _: "")
    assert out == ["claude-code", "codex", "copilot"]


def test_prompt_numbers_pick_specific_and_keep_order(installer: ModuleType) -> None:
    # "2,1" -> claude-code (1) + codex (2), returned in CLIENT_KEYS order.
    out = installer.prompt_clients(_detected_all_but_ai(), input_fn=lambda _: "2,1")
    assert out == ["claude-code", "codex"]


def test_prompt_quit_returns_empty(installer: ModuleType) -> None:
    assert installer.prompt_clients(_detected_all_but_ai(), input_fn=lambda _: "q") == []


def test_prompt_reprompts_on_garbage_then_accepts(installer: ModuleType) -> None:
    answers = iter(["nonsense", "copilot"])
    out = installer.prompt_clients(_detected_all_but_ai(), input_fn=lambda _: next(answers))
    assert out == ["copilot"]


# ── URL preservation (don't repoint a vault on a snippet refresh) ────────────


def test_pick_url_keeps_existing_when_not_explicit(installer: ModuleType) -> None:
    got = installer._pick_url(
        "https://v.fly.dev/mcp", "http://127.0.0.1:8765/mcp", url_explicit=False, label="x"
    )
    assert got == "https://v.fly.dev/mcp"


def test_pick_url_explicit_overrides(installer: ModuleType) -> None:
    got = installer._pick_url(
        "https://old.fly.dev/mcp", "https://new.fly.dev/mcp", url_explicit=True, label="x"
    )
    assert got == "https://new.fly.dev/mcp"


def test_pick_url_uses_default_when_no_existing(installer: ModuleType) -> None:
    got = installer._pick_url(None, "http://127.0.0.1:8765/mcp", url_explicit=False, label="x")
    assert got == "http://127.0.0.1:8765/mcp"


def test_cursor_keeps_vault_url_on_default_rerun(installer: ModuleType, home: Path) -> None:
    # The real footgun: re-running with the localhost default must NOT reset a
    # client that already points at a deployed vault.
    mcp = home / ".cursor" / "mcp.json"
    mcp.parent.mkdir(parents=True)
    mcp.write_text(
        json.dumps(
            {"mcpServers": {"afair": {"type": "http", "url": "https://myvault.fly.dev/mcp"}}}
        )
    )
    installer.install_cursor(
        token="", url="http://127.0.0.1:8765/mcp", dry=False, url_explicit=False
    )
    assert (
        json.loads(mcp.read_text())["mcpServers"]["afair"]["url"] == "https://myvault.fly.dev/mcp"
    )


def test_cursor_explicit_url_overrides_existing(installer: ModuleType, home: Path) -> None:
    mcp = home / ".cursor" / "mcp.json"
    mcp.parent.mkdir(parents=True)
    mcp.write_text(
        json.dumps({"mcpServers": {"afair": {"type": "http", "url": "https://old.fly.dev/mcp"}}})
    )
    installer.install_cursor(token="", url="https://new.fly.dev/mcp", dry=False, url_explicit=True)
    assert json.loads(mcp.read_text())["mcpServers"]["afair"]["url"] == "https://new.fly.dev/mcp"


def test_gemini_cli_keeps_vault_url_field_on_default_rerun(
    installer: ModuleType, home: Path
) -> None:
    # Same preservation, via the shared helper + a non-'url' field name.
    (home / ".gemini").mkdir()
    settings = home / ".gemini" / "settings.json"
    settings.write_text(
        json.dumps({"mcpServers": {"afair": {"httpUrl": "https://myvault.fly.dev/mcp"}}})
    )
    installer.install_gemini_cli(
        token="", url="http://127.0.0.1:8765/mcp", dry=False, url_explicit=False
    )
    assert (
        json.loads(settings.read_text())["mcpServers"]["afair"]["httpUrl"]
        == "https://myvault.fly.dev/mcp"
    )


# ── snippet: append / current / update-or-ask ────────────────────────────────


def test_ensure_snippet_appends_when_missing(installer: ModuleType, home: Path) -> None:
    md = home / "CLAUDE.md"
    md.write_text("# My rules\n")
    ch = installer._ensure_snippet(md, dry=False, update="no")
    assert ch is not None
    assert installer.SNIPPET_BODY in md.read_text()


def test_ensure_snippet_noop_when_current(installer: ModuleType, home: Path) -> None:
    md = home / "CLAUDE.md"
    md.write_text("# rules\n\n" + installer.SNIPPET_BODY + "\n")
    assert installer._ensure_snippet(md, dry=False, update="no") is None


def test_ensure_snippet_outdated_no_leaves_it(installer: ModuleType, home: Path) -> None:
    md = home / "CLAUDE.md"
    md.write_text("## afair: Persistent Memory Across AI Tools\n\nOLD OUTDATED\n")
    assert installer._ensure_snippet(md, dry=False, update="no") is None
    assert "OLD OUTDATED" in md.read_text()  # untouched without consent


def test_ensure_snippet_outdated_yes_replaces_block_only(installer: ModuleType, home: Path) -> None:
    md = home / "CLAUDE.md"
    md.write_text(
        "# top\n\n## afair: Persistent Memory Across AI Tools\n\nOLD\n\n## after\nkeep me\n"
    )
    ch = installer._ensure_snippet(md, dry=False, update="yes")
    text = md.read_text()
    assert ch is not None and ch.note == "updated"
    assert "OLD" not in text
    assert installer.SNIPPET_BODY in text
    assert "## after\nkeep me" in text  # the sibling section survives


def test_ensure_snippet_ask_declined_keeps(installer: ModuleType, home: Path) -> None:
    md = home / "CLAUDE.md"
    md.write_text("## afair: Persistent Memory Across AI Tools\n\nOLD\n")
    ch = installer._ensure_snippet(md, dry=False, update="ask", input_fn=lambda _: "n")
    assert ch is None and "OLD" in md.read_text()


def test_ensure_snippet_ask_accepted_updates(installer: ModuleType, home: Path) -> None:
    md = home / "CLAUDE.md"
    md.write_text("## afair: Persistent Memory Across AI Tools\n\nOLD\n")
    ch = installer._ensure_snippet(md, dry=False, update="ask", input_fn=lambda _: "y")
    assert ch is not None
    assert "OLD" not in md.read_text() and installer.SNIPPET_BODY in md.read_text()


# ── token lookup ─────────────────────────────────────────────────────────────


def test_load_token_returns_empty_when_absent(
    installer: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env.local in this cwd
    assert installer._load_token() == ""
