"""HTTP endpoints to mint, list and revoke user-minted API tokens.

Routes (all under /internal/tokens, all require the static
``AFAIR_AUTH_TOKEN`` master in the Authorization header — minted
sub-tokens cannot manage other tokens by design, so a leaked
agent token cannot mint more):

  GET    /internal/tokens          → JSON list of all tokens
  POST   /internal/tokens          → mint, returns plaintext ONCE
  DELETE /internal/tokens/<id>     → revoke
"""

from __future__ import annotations

import hmac
import json
import re
from typing import TYPE_CHECKING

import structlog
from starlette.responses import JSONResponse

from ..substrate import open_db
from . import api_tokens as _toks

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

log = structlog.get_logger(__name__)

_BEARER_RE = re.compile(r"^Bearer\s+(.+)$")
_MAX_LABEL = 80

# CORS allow-list for the /account dashboard, which loads from afair.ai
# but calls the per-user vault at <vanity>.mcp.afair.ai. Anything else
# is rejected — the master token is too sensitive to expose to arbitrary
# origins.
_ALLOWED_ORIGINS = frozenset(
    {
        "https://afair.ai",
        "http://localhost:3000",  # local dev for afair-web
    }
)


def _cors_headers(request: Request) -> dict[str, str]:
    """If the request comes from an allow-listed origin, return the
    matching CORS headers. Otherwise return an empty dict so the
    response includes nothing CORS-related (same-origin clients are
    unaffected).
    """
    origin = request.headers.get("origin", "")
    if origin in _ALLOWED_ORIGINS:
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
            "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
            "Access-Control-Max-Age": "300",
            "Vary": "Origin",
        }
    return {}


async def preflight_endpoint(request: Request) -> Response:
    """OPTIONS handler so browsers stop pre-failing the fetch."""
    return JSONResponse({}, headers=_cors_headers(request))


def _unauthorized(request: Request) -> Response:
    return JSONResponse(
        {"error": "unauthorized"},
        status_code=401,
        headers={
            "WWW-Authenticate": 'Bearer realm="tokens"',
            **_cors_headers(request),
        },
    )


def _check_master(request: Request) -> bool:
    """Only the static AFAIR_AUTH_TOKEN may manage other tokens.

    Sub-tokens (the ones this endpoint mints) cannot self-elevate by
    minting more tokens, even though they pass the main MCP bearer
    middleware. Constant-time compare.
    """
    settings = request.app.state.settings
    expected = settings.auth_token
    if expected is None:
        return False
    expected_value = expected.get_secret_value()
    header = request.headers.get("authorization", "")
    m = _BEARER_RE.match(header)
    if m is None:
        return False
    return hmac.compare_digest(m.group(1).strip(), expected_value)


def _conn(request: Request):  # type: ignore[no-untyped-def]
    """Open a substrate connection straight from settings.vault_dir.

    Same approach as the export endpoint: avoids depending on the
    request-scoped ``ServerContext`` so the route works in unit-test
    contexts that build a Starlette app without booting the full MCP
    server. open_db is cheap (SQLite open with WAL).
    """
    from pathlib import Path

    vault_dir = Path(request.app.state.settings.vault_dir)
    return open_db(vault_dir)


async def list_endpoint(request: Request) -> Response:
    if not _check_master(request):
        return _unauthorized(request)
    tokens = _toks.list_all(_conn(request))
    return JSONResponse(
        {
            "tokens": [
                {
                    "id": t.id,
                    "label": t.label,
                    "scope": t.scope,
                    "created_at": t.created_at,
                    "last_used_at": t.last_used_at,
                    "revoked": t.revoked,
                }
                for t in tokens
            ],
        },
        headers=_cors_headers(request),
    )


async def mint_endpoint(request: Request) -> Response:
    if not _check_master(request):
        return _unauthorized(request)
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be an object"}, status_code=400)
    label = body.get("label")
    scope = body.get("scope", "full")
    if not isinstance(label, str) or not label.strip():
        return JSONResponse(
            {"error": "label is required (non-empty string)"},
            status_code=400,
        )
    if len(label) > _MAX_LABEL:
        return JSONResponse(
            {"error": f"label too long (max {_MAX_LABEL} chars)"},
            status_code=400,
        )
    if scope not in ("full", "read", "write"):
        return JSONResponse(
            {"error": "scope must be one of: full, read, write"},
            status_code=400,
        )
    minted = _toks.mint(_conn(request), label=label, scope=scope)
    log.info(
        "api_tokens.minted",
        token_id=minted.id,
        label=minted.label,
        scope=minted.scope,
    )
    return JSONResponse(
        {
            "id": minted.id,
            "label": minted.label,
            "scope": minted.scope,
            "created_at": minted.created_at,
            # Plaintext returned exactly ONCE. Tell the caller this in
            # the response so a UI can render the right warning.
            "token": minted.plaintext,
            "note": "Save this token now. It is shown only this once.",
        },
        status_code=201,
        headers=_cors_headers(request),
    )


async def revoke_endpoint(request: Request) -> Response:
    if not _check_master(request):
        return _unauthorized(request)
    token_id = request.path_params.get("token_id", "")
    if not token_id or not token_id.startswith("tok_"):
        return JSONResponse({"error": "invalid token id"}, status_code=400)
    flipped = _toks.revoke(_conn(request), token_id)
    log.info("api_tokens.revoke_requested", token_id=token_id, flipped=flipped)
    return JSONResponse(
        {"id": token_id, "revoked": True, "was_active": flipped},
        headers=_cors_headers(request),
    )
