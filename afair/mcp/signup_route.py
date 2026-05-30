"""Scoped signup endpoint for the public landing-page form.

Closes Security audit finding I7. The afair-web Next.js landing app
needs to write early-access signup events into the substrate, but
giving it the full ``AFAIR_AUTH_TOKEN`` (which can ``remember`` /
``recall`` / ``observe`` anything) means a compromise of the public
web app yields complete vault access.

This endpoint is a narrow surface: one method (POST), one event shape
(remember-style with a fixed context + type_hint), one credential
(``AFAIR_SIGNUP_TOKEN``). The web app gets a token that only opens
this door; nothing else.

Mounted at ``/internal/signup`` so it lives OUTSIDE the MCP namespace
and is trivially distinguishable in logs + access patterns.
"""

from __future__ import annotations

import hmac
import re
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, EmailStr, Field
from starlette.responses import JSONResponse

from ..substrate import write_event
from .context import connect_for_thread

if TYPE_CHECKING:
    from starlette.requests import Request

log = structlog.get_logger(__name__)


SIGNUP_CONTEXT = "afair.ai early-access signup"
SIGNUP_TYPE_HINT = "signup"
SIGNUP_ORIGIN = "channel:landing"


class SignupRequest(BaseModel):
    """The wire shape the landing-page form posts."""

    email: EmailStr
    # Optional referral / channel tag for attribution. Truncated server-side.
    source: str | None = Field(default=None, max_length=80)


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        {"error": "unauthorized"},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer realm="signup"'},
    )


_TOKEN_RE = re.compile(r"^Bearer\s+(.+)$")


async def signup_endpoint(request: Request) -> JSONResponse:
    """POST /internal/signup — restricted to the signup-scoped bearer.

    Auth: Authorization: Bearer <AFAIR_SIGNUP_TOKEN>. Constant-time
    comparison; no fallthrough to the general bearer.

    On success: writes ONE event with the standard signup shape and
    returns ``{"ok": true, "event_id": "<ulid>"}``. The whole point of
    the narrow scope is that the response is also narrow — no recall
    data is ever returned by this endpoint.
    """
    settings = request.app.state.settings
    expected = settings.signup_token
    if expected is None:
        return JSONResponse(
            {"error": "signup_endpoint_disabled"},
            status_code=503,
        )

    auth_header = request.headers.get("authorization", "")
    match = _TOKEN_RE.match(auth_header)
    if match is None:
        return _unauthorized()
    presented = match.group(1).strip()
    if not hmac.compare_digest(presented, expected.get_secret_value()):
        return _unauthorized()

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)

    try:
        parsed = SignupRequest.model_validate(body)
    except Exception as e:
        return JSONResponse(
            {"error": "invalid_request", "detail": str(e)[:200]},
            status_code=400,
        )

    payload: dict[str, object] = {
        "content_type": "text",
        "text": str(parsed.email),
        "context": SIGNUP_CONTEXT,
        "type_hint": SIGNUP_TYPE_HINT,
    }
    if parsed.source:
        payload["source"] = parsed.source

    db = connect_for_thread()
    event = write_event(
        db,
        origin=SIGNUP_ORIGIN,
        kind="remember",
        payload=payload,
    )

    log.info(
        "signup.recorded",
        event_id=event.id,
        content_hash=event.content_hash,
        source=parsed.source,
    )
    return JSONResponse(
        {"ok": True, "event_id": event.id, "content_hash": event.content_hash},
        status_code=200,
    )
