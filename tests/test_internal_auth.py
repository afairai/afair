"""Tests for dashboard-session auth on the browser-facing /internal routes.

The /account dashboard authorises against the user's own vault with a
short-lived JWT minted by afair-web from the Clerk session (intent
"dashboard"), instead of a pasted master bearer. These tests pin that the
vault accepts a correctly-signed, correctly-scoped token and rejects every
way it can be wrong.
"""

from __future__ import annotations

import base64
import hmac
import json
import time
from hashlib import sha256
from typing import TYPE_CHECKING

from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from afair.mcp.export_async_routes import export_status_endpoint
from afair.settings import Settings

if TYPE_CHECKING:
    from pathlib import Path

HUB_SECRET = "hub-shared-secret"
ISSUER = "https://your-app.mcp.afair.ai"
USER = "user_TESToperator00000000000000"


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _mint(
    secret: str,
    *,
    sub: str = USER,
    intent: str = "dashboard",
    return_to: str = ISSUER,
    exp_delta: int = 120,
) -> str:
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": sub,
        "email": None,
        "intent": intent,
        "return_to": return_to,
        "iat": now,
        "exp": now + exp_delta,
    }
    h = _b64url(json.dumps(header).encode())
    p = _b64url(json.dumps(payload).encode())
    sig = hmac.new(secret.encode(), f"{h}.{p}".encode(), sha256).digest()
    return f"{h}.{p}.{_b64url(sig)}"


def _build_app(vault_dir: Path) -> Starlette:
    settings = Settings(
        vault_dir=vault_dir,
        afair_auth_token=SecretStr("master"),
        identity_hub_secret=SecretStr(HUB_SECRET),
        oauth_issuer=ISSUER,
        identity_allowlist=USER,
    )
    app = Starlette(
        routes=[Route("/internal/export/status", export_status_endpoint, methods=["GET"])]
    )
    app.state.settings = settings
    return app


def _get(app: Starlette, token: str):
    return TestClient(app).get(
        "/internal/export/status", headers={"Authorization": f"Bearer {token}"}
    )


def test_valid_dashboard_jwt_authorizes(tmp_path) -> None:
    app = _build_app(tmp_path)
    r = _get(app, _mint(HUB_SECRET))
    assert r.status_code == 200


def test_master_bearer_still_authorizes(tmp_path) -> None:
    app = _build_app(tmp_path)
    r = _get(app, "master")
    assert r.status_code == 200


def test_wrong_signature_rejected(tmp_path) -> None:
    app = _build_app(tmp_path)
    r = _get(app, _mint("not-the-hub-secret"))
    assert r.status_code == 401


def test_wrong_intent_rejected(tmp_path) -> None:
    # A token minted for the MCP-login flow must not authorise the dashboard
    # management routes.
    app = _build_app(tmp_path)
    r = _get(app, _mint(HUB_SECRET, intent="mcp"))
    assert r.status_code == 401


def test_wrong_return_to_rejected(tmp_path) -> None:
    # A token for a different machine's issuer can't be replayed here.
    app = _build_app(tmp_path)
    r = _get(app, _mint(HUB_SECRET, return_to="https://other-host.mcp.afair.ai"))
    assert r.status_code == 401


def test_other_user_sub_rejected(tmp_path) -> None:
    # Single-tenant (I8): a validly-signed token for a different identity is
    # rejected because its sub is not this machine's allow-listed user.
    app = _build_app(tmp_path)
    r = _get(app, _mint(HUB_SECRET, sub="user_someoneElse"))
    assert r.status_code == 401


def test_expired_jwt_rejected(tmp_path) -> None:
    app = _build_app(tmp_path)
    r = _get(app, _mint(HUB_SECRET, exp_delta=-3600))
    assert r.status_code == 401
