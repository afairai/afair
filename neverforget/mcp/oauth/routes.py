"""OAuth 2.1 server routes — the actual HTTP surface.

Endpoints exposed:
  GET  /.well-known/oauth-protected-resource    metadata (no auth)
  GET  /.well-known/oauth-authorization-server  metadata (no auth)
  POST /oauth/register                          DCR (RFC 7591)
  GET  /oauth/authorize                         start the flow
  GET  /oauth/identity/github/callback          identity-backend callback
  POST /oauth/token                             code → JWT access token
  POST /oauth/revoke                            revoke a refresh token

The actual identity verification is delegated to a pluggable backend
(Phase 1: GitHub OAuth). The allowlist check is enforced at /callback
time — unrecognized GitHub usernames get rejected before any code is
issued.
"""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.parse
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse, RedirectResponse, Response

from ...substrate import open_db
from . import jwt as jwt_mod
from . import metadata, storage
from .identity import GitHubIdentityBackend, IdentityBackend

if TYPE_CHECKING:
    from starlette.requests import Request

    from ...settings import Settings


# ── helpers ─────────────────────────────────────────────────────────────────


def _error(
    error: str,
    *,
    description: str | None = None,
    status: int = 400,
) -> JSONResponse:
    """Standard OAuth 2.1 error response envelope (RFC 6749 §5.2)."""
    body: dict[str, str] = {"error": error}
    if description:
        body["error_description"] = description
    return JSONResponse(body, status_code=status)


def _verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool:
    """Compare hash(code_verifier) against the stored code_challenge."""
    if method != "S256":
        return False
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("utf-8")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    import hmac

    return hmac.compare_digest(expected, code_challenge)


def _identity_backend(settings: Settings) -> IdentityBackend:
    """Construct the configured identity backend. Currently github-only."""
    if settings.identity_backend != "github":
        msg = f"unsupported identity_backend: {settings.identity_backend!r}"
        raise ValueError(msg)
    if settings.github_oauth_client_id is None or settings.github_oauth_client_secret is None:
        msg = "GitHub identity backend requires GITHUB_OAUTH_CLIENT_ID + SECRET"
        raise ValueError(msg)
    return GitHubIdentityBackend(
        client_id=settings.github_oauth_client_id.get_secret_value(),
        client_secret=settings.github_oauth_client_secret.get_secret_value(),
    )


def _identity_callback_url(settings: Settings) -> str:
    return f"{settings.effective_oauth_issuer}/oauth/identity/github/callback"


# ── metadata ────────────────────────────────────────────────────────────────


async def well_known_oauth_protected_resource(
    request: Request,
) -> JSONResponse:
    settings: Settings = request.app.state.settings
    return JSONResponse(metadata.protected_resource_metadata(settings))


async def well_known_oauth_authorization_server(
    request: Request,
) -> JSONResponse:
    settings: Settings = request.app.state.settings
    return JSONResponse(metadata.authorization_server_metadata(settings))


# ── DCR ─────────────────────────────────────────────────────────────────────


async def oauth_register(request: Request) -> Response:
    """RFC 7591 Dynamic Client Registration.

    We accept any client. The client picks its own ``redirect_uris``;
    no human-in-the-loop. Returned client_id + (optional) client_secret
    are then used by the client to authenticate with /oauth/token.
    """
    settings: Settings = request.app.state.settings
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _error("invalid_client_metadata", description="body must be JSON")

    if not isinstance(body, dict):
        return _error("invalid_client_metadata", description="body must be a JSON object")

    redirect_uris = body.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return _error(
            "invalid_redirect_uri",
            description="redirect_uris is required and must be a non-empty array",
        )
    if not all(isinstance(u, str) and u for u in redirect_uris):
        return _error("invalid_redirect_uri", description="redirect_uris must be strings")

    client_name = body.get("client_name") if isinstance(body.get("client_name"), str) else None
    # Default to public client (PKCE-only, no secret). Some MCP clients
    # request confidential by setting token_endpoint_auth_method.
    confidential = body.get("token_endpoint_auth_method") == "client_secret_post"

    db = open_db(settings.vault_dir)
    try:
        client, secret = storage.register_client(
            db,
            redirect_uris=redirect_uris,
            client_name=client_name,
            confidential=confidential,
            metadata=body,
        )
    finally:
        db.close()

    response: dict[str, object] = {
        "client_id": client.client_id,
        "redirect_uris": list(client.redirect_uris),
        "token_endpoint_auth_method": "client_secret_post" if confidential else "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "client_name": client_name,
    }
    if secret is not None:
        response["client_secret"] = secret
    return JSONResponse(response, status_code=201)


# ── authorize: start the flow ───────────────────────────────────────────────


async def oauth_authorize(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    params = request.query_params

    response_type = params.get("response_type", "")
    if response_type != "code":
        return _error("unsupported_response_type", description="only 'code' is supported")

    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    code_challenge = params.get("code_challenge", "")
    code_challenge_method = params.get("code_challenge_method", "S256")
    scope = params.get("scope")
    client_state = params.get("state")

    if not client_id or not redirect_uri or not code_challenge:
        return _error(
            "invalid_request",
            description="client_id, redirect_uri, code_challenge are required",
        )
    if code_challenge_method != "S256":
        return _error(
            "invalid_request",
            description="only S256 code_challenge_method is supported",
        )

    db = open_db(settings.vault_dir)
    try:
        client = storage.get_client(db, client_id)
        if client is None:
            return _error("invalid_client", description="unknown client_id", status=401)
        if redirect_uri not in client.redirect_uris:
            return _error(
                "invalid_redirect_uri",
                description="redirect_uri not registered for this client",
            )
        # Save the in-flight dance state, indexed by our internal state token.
        login = storage.save_login_state(
            db,
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope=scope,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            client_state=client_state,
        )
    finally:
        db.close()

    backend = _identity_backend(settings)
    backend_redirect = _identity_callback_url(settings)
    auth_url = await backend.authorize_url(state=login.state, redirect_uri=backend_redirect)
    return RedirectResponse(auth_url, status_code=302)


# ── identity callback: GitHub redirects back to us ──────────────────────────


async def oauth_identity_github_callback(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    params = request.query_params

    if "error" in params:
        return _error(
            params["error"],
            description=params.get("error_description", "identity backend returned an error"),
        )

    code = params.get("code")
    state = params.get("state")
    if not code or not state:
        return _error(
            "invalid_request",
            description="GitHub callback missing code or state",
        )

    db = open_db(settings.vault_dir)
    try:
        login = storage.consume_login_state(db, state)
        if login is None:
            return _error(
                "invalid_request",
                description="unknown or expired state",
                status=400,
            )

        backend = _identity_backend(settings)
        user = await backend.fetch_user(
            code=code,
            redirect_uri=_identity_callback_url(settings),
        )

        # Allowlist enforcement (I8 single-tenant — exactly one allowed user).
        if user.sub.lower() not in settings.allowlist:
            return _error(
                "access_denied",
                description=(f"identity '{user.sub}' is not in the allowlist for this instance"),
                status=403,
            )

        # Mint OUR authorization code (separate from GitHub's).
        our_code = storage.save_authorization_code(
            db,
            client_id=login.client_id,
            redirect_uri=login.redirect_uri,
            scope=login.scope,
            code_challenge=login.code_challenge,
            code_challenge_method=login.code_challenge_method,
            user_sub=user.sub,
            user_email=user.email,
        )
    finally:
        db.close()

    # Build redirect back to the original MCP client with our auth code.
    qs_parts: dict[str, str] = {"code": our_code.code}
    if login.client_state:
        qs_parts["state"] = login.client_state
    separator = "&" if "?" in login.redirect_uri else "?"
    return RedirectResponse(
        f"{login.redirect_uri}{separator}{urllib.parse.urlencode(qs_parts)}",
        status_code=302,
    )


# ── token: exchange code → JWT ──────────────────────────────────────────────


async def oauth_token(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    form = await request.form()

    grant_type = form.get("grant_type")
    if grant_type == "authorization_code":
        return await _grant_authorization_code(request, settings, form)
    if grant_type == "refresh_token":
        return await _grant_refresh_token(settings, form)
    return _error("unsupported_grant_type", description=f"grant_type={grant_type!r}")


async def _grant_authorization_code(
    request: Request,
    settings: Settings,
    form: object,
) -> Response:
    code = _form_str(form, "code")
    redirect_uri = _form_str(form, "redirect_uri")
    code_verifier = _form_str(form, "code_verifier")
    client_id = _form_str(form, "client_id")

    if not code or not redirect_uri or not code_verifier or not client_id:
        return _error(
            "invalid_request",
            description="code, redirect_uri, code_verifier, client_id are required",
        )

    db = open_db(settings.vault_dir)
    try:
        client = storage.get_client(db, client_id)
        if client is None:
            return _error("invalid_client", status=401)

        # Confidential client → verify secret
        if client.has_secret:
            secret = _form_str(form, "client_secret")
            if not secret or not storage.verify_client_secret(db, client_id, secret):
                return _error(
                    "invalid_client",
                    description="client_secret missing or wrong",
                    status=401,
                )

        ac = storage.consume_authorization_code(db, code)
        if ac is None:
            return _error("invalid_grant", description="unknown or expired code")
        if ac.client_id != client_id:
            return _error("invalid_grant", description="code does not match client")
        if ac.redirect_uri != redirect_uri:
            return _error("invalid_grant", description="redirect_uri mismatch")
        if not _verify_pkce(code_verifier, ac.code_challenge, ac.code_challenge_method):
            return _error("invalid_grant", description="PKCE verification failed")

        issued = jwt_mod.issue_access_token(
            settings=settings, subject=ac.user_sub, email=ac.user_email
        )
        refresh = storage.issue_refresh_token(
            db,
            client_id=client_id,
            user_sub=ac.user_sub,
            scope=ac.scope,
            ttl_seconds=settings.refresh_token_ttl_seconds,
        )
    finally:
        db.close()

    return JSONResponse(
        {
            "access_token": issued.token,
            "token_type": "Bearer",
            "expires_in": settings.access_token_ttl_seconds,
            "refresh_token": refresh,
            "scope": ac.scope or "",
        }
    )


async def _grant_refresh_token(settings: Settings, form: object) -> Response:
    token = _form_str(form, "refresh_token")
    if not token:
        return _error("invalid_request", description="refresh_token is required")

    db = open_db(settings.vault_dir)
    try:
        record = storage.lookup_refresh_token(db, token)
        if record is None:
            return _error("invalid_grant", description="invalid or expired refresh_token")
        # Re-check allowlist (operator may have removed the user since issuance)
        if record.user_sub.lower() not in settings.allowlist:
            storage.revoke_refresh_token(db, token)
            return _error("invalid_grant", description="user no longer permitted")

        issued = jwt_mod.issue_access_token(settings=settings, subject=record.user_sub, email=None)
    finally:
        db.close()

    return JSONResponse(
        {
            "access_token": issued.token,
            "token_type": "Bearer",
            "expires_in": settings.access_token_ttl_seconds,
            "scope": record.scope or "",
        }
    )


# ── revoke ──────────────────────────────────────────────────────────────────


async def oauth_revoke(request: Request) -> Response:
    """RFC 7009. Always returns 200 regardless of token validity."""
    settings: Settings = request.app.state.settings
    form = await request.form()
    token = _form_str(form, "token")
    if token:
        db = open_db(settings.vault_dir)
        try:
            storage.revoke_refresh_token(db, token)
        finally:
            db.close()
    return Response(status_code=200)


# ── form helpers ────────────────────────────────────────────────────────────


def _form_str(form: object, key: str) -> str:
    # Starlette's Form is a multi-dict; .get returns string or UploadFile.
    if hasattr(form, "get"):
        value = form.get(key)
        if isinstance(value, str):
            return value
    return ""
