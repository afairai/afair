"""Authorization for the browser-facing /internal routes.

The /account dashboard at afair.ai needs to manage API tokens and drive the
async export against the user's own vault. Two credentials are accepted, in
order:

  1. the static master bearer (AFAIR_AUTH_TOKEN) — what headless/CLI clients
     and power users still use; and
  2. a short-lived **dashboard JWT** minted by afair-web from the user's
     Clerk session (intent="dashboard", signed with the shared
     IDENTITY_HUB_SECRET, sub == the single allow-listed identity).

(2) is what removes the "paste your onboarding bearer" friction: when the
user is logged into the dashboard, afair-web mints this token server-side
and the browser uses it. The vault trusts it exactly like it already trusts
the hub's MCP-login tokens — same signer, same verifier, just a distinct
intent so a dashboard credential can't be replayed as an MCP login.

Single-tenant (I8): the JWT's sub must equal this machine's one allow-listed
identity, so even a validly-signed token for a different user is rejected.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .oauth.identity_hub import verify_identity_token

if TYPE_CHECKING:
    from starlette.requests import Request

_BEARER_RE = re.compile(r"^Bearer\s+(.+)$")

DASHBOARD_INTENT = "dashboard"


def _bearer(request: Request) -> str | None:
    m = _BEARER_RE.match(request.headers.get("authorization", ""))
    return m.group(1).strip() if m else None


def check_master(request: Request) -> bool:
    """Constant-time match against the static master bearer."""
    import hmac

    settings = request.app.state.settings
    expected = settings.auth_token
    presented = _bearer(request)
    if expected is None or presented is None:
        return False
    return hmac.compare_digest(presented, expected.get_secret_value())


def check_session(request: Request) -> bool:
    """Accept a dashboard JWT minted by afair-web from the Clerk session.

    Verified with IDENTITY_HUB_SECRET, intent must be "dashboard", the
    return_to/audience must be THIS machine's issuer, and the sub must be the
    single allow-listed identity (single-tenant). Any failure → False.
    """
    settings = request.app.state.settings
    secret = settings.identity_hub_secret
    if secret is None:
        return False
    token = _bearer(request)
    if token is None:
        return False
    payload = verify_identity_token(
        token,
        secret=secret.get_secret_value(),
        expected_return_to=settings.effective_oauth_issuer,
        expected_intent=DASHBOARD_INTENT,
    )
    if payload is None:
        return False
    # Single-tenant: the token's subject must be this machine's user.
    return payload.sub.lower() in settings.allowlist


def authorize_internal(request: Request) -> bool:
    """Master bearer OR dashboard session — either authorises the
    browser-facing /internal management routes."""
    return check_master(request) or check_session(request)
