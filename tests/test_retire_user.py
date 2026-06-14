"""Tests for the canonical user-teardown script (scripts/retire_user.py).

retire_user.py is the single teardown path shared by the grace-period
cron and the instant user-initiated delete. These tests pin:
  - it derives the SAME app/host as provision_user (never targets the
    wrong machine),
  - dry-run touches nothing remote,
  - the Fly destroy is idempotent (skips a gone app),
  - the Cloudflare delete is a lookup-then-delete inverse of create,
  - the callback payload shape afair-web's /api/internal/retired expects.
"""

from __future__ import annotations

import io
import json
from typing import TYPE_CHECKING, Any

from scripts import provision_user, retire_user

if TYPE_CHECKING:
    import pytest


def test_app_and_host_match_provisioning() -> None:
    """A retire must target exactly what provisioning built — same hash,
    same names. If these ever diverge, a teardown would orphan the real
    app and destroy nothing (or worse, the wrong thing)."""
    identity = "user_TESToperator00000000000000"
    assert retire_user.app_name_for(identity) == provision_user.app_name_for(identity)
    assert retire_user.vanity_host_for(identity) == provision_user.vanity_host_for(identity)


def test_dry_run_touches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dry-run prints the plan and makes zero remote calls."""
    calls: list[str] = []

    def _boom(*_a: Any, **_k: Any) -> Any:
        calls.append("remote")
        raise AssertionError("dry-run must not hit the network / flyctl")

    monkeypatch.setattr(retire_user.subprocess, "run", _boom)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token")
    monkeypatch.setenv("RETIRE_CALLBACK_SECRET", "secret")

    app, vanity = retire_user.retire(identity="user_abc", reason="user-requested", dry=True)
    assert app == retire_user.app_name_for("user_abc")
    assert vanity == retire_user.vanity_host_for("user_abc")
    assert calls == []


def test_fly_destroy_skips_gone_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """An already-gone app is a no-op (idempotent re-run path)."""
    monkeypatch.setattr(retire_user, "fly_app_exists", lambda _app: False)
    ran: list[list[str]] = []
    monkeypatch.setattr(retire_user.subprocess, "run", lambda cmd, **_k: ran.append(cmd))
    issued = retire_user.fly_destroy_app("afair-vega-7a3", dry=False)
    assert issued is False
    assert ran == []  # never called flyctl apps destroy


def test_fly_destroy_runs_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retire_user, "fly_app_exists", lambda _app: True)
    ran: list[list[str]] = []
    monkeypatch.setattr(retire_user.subprocess, "run", lambda cmd, **_k: ran.append(cmd))
    issued = retire_user.fly_destroy_app("afair-vega-7a3", dry=False)
    assert issued is True
    assert ran and ran[0][:3] == ["flyctl", "apps", "destroy"]
    assert "--yes" in ran[0]


def _fake_urlopen(responses: list[dict[str, Any]]) -> Any:
    """Return a urlopen stub yielding the given JSON bodies in order,
    recording each request (method + url) on the returned object."""
    seen: list[tuple[str, str]] = []
    it = iter(responses)

    class _Resp(io.BytesIO):
        status = 200

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

    def _open(req: Any, timeout: int = 0) -> _Resp:
        seen.append((req.get_method(), req.full_url))
        return _Resp(json.dumps(next(it)).encode("utf-8"))

    _open.seen = seen  # type: ignore[attr-defined]
    return _open


def test_cloudflare_delete_removes_matching_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lookup by name returns one record; we DELETE it by id."""
    import urllib.request

    fake = _fake_urlopen(
        [
            {"result": [{"id": "rec_123"}]},  # list lookup
            {"success": True},  # delete
        ]
    )
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    removed = retire_user.cloudflare_delete_cname(
        hostname="vega-7a3.mcp.afair.ai", token="cf", dry=False
    )
    assert removed == 1
    methods = [m for m, _ in fake.seen]  # type: ignore[attr-defined]
    assert methods == ["GET", "DELETE"]


def test_cloudflare_delete_noop_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """No matching record → zero deletes (idempotent second pass)."""
    import urllib.request

    fake = _fake_urlopen([{"result": []}])
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    removed = retire_user.cloudflare_delete_cname(
        hostname="vega-7a3.mcp.afair.ai", token="cf", dry=False
    )
    assert removed == 0
    assert [m for m, _ in fake.seen] == ["GET"]  # type: ignore[attr-defined]


def test_callback_payload_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retired callback POSTs {clerk_user_id, fly_app, reason} with a
    Bearer header — exactly what /api/internal/retired parses."""
    import urllib.request

    captured: dict[str, Any] = {}

    class _Resp(io.BytesIO):
        status = 200

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

    def _open(req: Any, timeout: int = 0) -> _Resp:
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp(b"{}")

    monkeypatch.setattr(urllib.request, "urlopen", _open)
    monkeypatch.setenv("RETIRE_CALLBACK_SECRET", "shh")
    monkeypatch.setenv("RETIRE_CALLBACK_URL", "https://afair.ai/api/internal/retired")

    ok = retire_user.notify_retired(
        identity="user_abc", app="afair-vega-7a3", reason="user-requested", dry=False
    )
    assert ok is True
    assert captured["url"] == "https://afair.ai/api/internal/retired"
    assert captured["auth"] == "Bearer shh"
    assert captured["body"] == {
        "clerk_user_id": "user_abc",
        "fly_app": "afair-vega-7a3",
        "reason": "user-requested",
    }


def test_callback_skipped_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RETIRE_CALLBACK_SECRET", raising=False)
    ok = retire_user.notify_retired(
        identity="user_abc", app="afair-vega-7a3", reason="x", dry=False
    )
    assert ok is False


def test_keep_dns_leaves_cname(monkeypatch: pytest.MonkeyPatch) -> None:
    """--keep-dns destroys the app but never calls Cloudflare."""
    monkeypatch.setattr(retire_user, "fly_app_exists", lambda _app: True)
    monkeypatch.setattr(retire_user.subprocess, "run", lambda *_a, **_k: None)

    def _no_cf(*_a: Any, **_k: Any) -> int:
        raise AssertionError("--keep-dns must not touch Cloudflare")

    monkeypatch.setattr(retire_user, "cloudflare_delete_cname", _no_cf)
    monkeypatch.setattr(retire_user, "notify_retired", lambda **_k: True)

    app, _ = retire_user.retire(identity="user_abc", reason="x", dry=False, keep_dns=True)
    assert app == retire_user.app_name_for("user_abc")
