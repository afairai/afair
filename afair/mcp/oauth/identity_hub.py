"""
Identity-hub client — verifies signed identity tokens issued by
``https://afair.ai/oauth/identity/github/callback``.

The MCP server no longer talks to GitHub directly. It delegates GitHub
OAuth verification to afair.ai (the identity hub), and receives a
signed JWT at its ``/oauth/identity/accept`` endpoint. That JWT
contains the verified GitHub username (``sub``), the audience this
token was issued for (``return_to``), and a short expiry (5 minutes).

Why federate:
  * one GitHub OAuth App handles every afair surface (admin, this
    MCP server, future per-user MCP servers ``u-<hash>.mcp.afair.ai``,
    customer-facing login on ``app.afair.ai``);
  * the OAuth App's single registered callback URL is on afair.ai;
  * each consuming server (this one included) trusts the hub via a
    shared HMAC secret rather than implementing OAuth itself.

Trust model:
  * IDENTITY_HUB_SECRET is the HMAC-SHA-256 key, shared between this
    server and the hub. Rotating it invalidates in-flight tokens
    (acceptable — they live 5 minutes).
  * The ``return_to`` claim in the JWT MUST match this server's
    accept URL. A token issued for another server cannot be replayed
    here.
  * The ``intent`` claim must be ``"mcp"`` (admin tokens are bound
    to ``intent="admin"`` and never reach this endpoint).

Token format: HS256 mini-JWT, three base64url parts:
    header.payload.signature
Payload: {sub, email, intent, return_to, iat, exp}.
"""
from __future__ import annotations

import base64
import hmac
import json
import time
from dataclasses import dataclass
from hashlib import sha256


@dataclass(frozen=True)
class IdentityPayload:
    sub: str
    email: str | None
    intent: str
    return_to: str
    iat: int
    exp: int


def _b64url_decode(s: str) -> bytes:
    # base64url with no padding — pad to multiple of 4 before decoding.
    pad = (-len(s)) % 4
    return base64.urlsafe_b64decode(s + ("=" * pad))


def verify_identity_token(
    token: str,
    *,
    secret: str,
    expected_return_to: str,
    expected_intent: str = "mcp",
    leeway_seconds: int = 30,
) -> IdentityPayload | None:
    """Verify a hub-issued identity token.

    Returns the parsed payload on success, ``None`` on any failure
    (bad signature, expired, wrong audience, wrong intent, malformed
    JSON, missing fields). Callers should treat ``None`` as "do not
    proceed" — never partial trust.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    h_b64, p_b64, s_b64 = parts

    expected_sig = hmac.new(
        secret.encode("utf-8"),
        f"{h_b64}.{p_b64}".encode(),
        sha256,
    ).digest()
    try:
        got_sig = _b64url_decode(s_b64)
    except Exception:
        return None
    if not hmac.compare_digest(got_sig, expected_sig):
        return None

    try:
        payload_raw = _b64url_decode(p_b64).decode("utf-8")
        payload = json.loads(payload_raw)
    except Exception:
        return None

    # Required fields.
    sub = payload.get("sub")
    intent = payload.get("intent")
    return_to = payload.get("return_to")
    exp = payload.get("exp")
    iat = payload.get("iat")
    email = payload.get("email")

    if not isinstance(sub, str) or not sub:
        return None
    if intent != expected_intent:
        return None
    if return_to != expected_return_to:
        return None
    if not isinstance(exp, int):
        return None
    if int(time.time()) - leeway_seconds > exp:
        return None
    # iat / email are best-effort
    if not isinstance(iat, int):
        iat = 0
    if email is not None and not isinstance(email, str):
        email = None

    return IdentityPayload(
        sub=sub,
        email=email,
        intent=intent,
        return_to=return_to,
        iat=iat,
        exp=exp,
    )


def hub_start_url(*, hub_url: str, intent: str, return_to: str, state: str) -> str:
    """Build the URL to start the identity-hub flow.

    Callers (oauth_authorize) redirect the user-agent to this URL.
    The hub will:
      1. drive GitHub OAuth;
      2. mint an identity JWT with audience=return_to and intent=intent;
      3. redirect the user back to return_to?token=<jwt>&state=<state>.
    """
    from urllib.parse import urlencode

    base = hub_url.rstrip("/")
    params = urlencode({"intent": intent, "return_to": return_to, "state": state})
    return f"{base}/oauth/identity/start?{params}"
