"""JWT issuance + validation for the MCP OAuth surface.

Phase 1: HS256 signed with a server-side secret. Simple, single-tenant,
works without a keypair management story. Upgrade to RS256 with a
published JWKS endpoint in Phase 8 when multiple instances need to share
issuer trust.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import jwt

if TYPE_CHECKING:
    from ...settings import Settings

# Standard JWT algorithms we support. HS256 is symmetric (shared secret);
# RS256 would need a keypair and a JWKS endpoint — Phase 8 work.
_ALGORITHM = "HS256"


class JWTError(Exception):
    """Base class for JWT problems."""


class JWTExpired(JWTError):
    pass


class JWTInvalid(JWTError):
    pass


@dataclass(frozen=True)
class IssuedToken:
    """An access token plus its metadata. Returned by ``issue()``."""

    token: str
    expires_at: int  # unix timestamp
    issued_at: int
    subject: str
    audience: str


@dataclass(frozen=True)
class TokenClaims:
    """A parsed + validated JWT. Returned by ``validate()``."""

    sub: str  # identity subject (e.g., GitHub username)
    aud: str  # audience (which MCP server this token is valid for)
    iss: str  # issuer
    exp: int
    iat: int
    jti: str  # JWT ID — useful for revocation
    email: str | None = None  # GitHub user's email (advisory)


def issue_access_token(
    *,
    settings: Settings,
    subject: str,
    email: str | None,
    audience: str | None = None,
) -> IssuedToken:
    """Mint a short-lived access token for ``subject``.

    ``audience`` defaults to the MCP resource URL (``<issuer>/mcp``) per
    RFC 8707 + the MCP authorization spec — tokens are bound to a specific
    resource, NOT to the authorization server's issuer URL. Strict clients
    (Claude.ai included) validate ``aud`` against the resource they intend
    to call and silently reject on mismatch.
    """
    if settings.jwt_secret is None:
        msg = "NEVERFORGET_JWT_SECRET must be set to issue tokens"
        raise JWTError(msg)

    now = int(time.time())
    exp = now + settings.access_token_ttl_seconds
    iss = settings.effective_oauth_issuer
    # Default audience is the MCP endpoint URL — the canonical resource
    # identifier per RFC 8707. Callers can override (e.g., when the client
    # explicitly requested a different resource via the `resource` param).
    aud = audience or f"{iss}/mcp"
    jti = secrets.token_urlsafe(16)

    payload = {
        "iss": iss,
        "aud": aud,
        "sub": subject,
        "iat": now,
        "exp": exp,
        "jti": jti,
    }
    if email:
        payload["email"] = email

    token = jwt.encode(
        payload,
        settings.jwt_secret.get_secret_value(),
        algorithm=_ALGORITHM,
    )
    return IssuedToken(
        token=token,
        expires_at=exp,
        issued_at=now,
        subject=subject,
        audience=aud,
    )


def validate(
    token: str,
    *,
    settings: Settings,
    expected_audience: str | None = None,
) -> TokenClaims:
    """Parse + verify a JWT. Raises on any failure."""
    if settings.jwt_secret is None:
        msg = "NEVERFORGET_JWT_SECRET must be set to validate tokens"
        raise JWTError(msg)

    expected_iss = settings.effective_oauth_issuer
    # Default expected audience is the MCP endpoint (per RFC 8707).
    expected_aud = expected_audience or f"{expected_iss}/mcp"

    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[_ALGORITHM],
            audience=expected_aud,
            issuer=expected_iss,
            options={"require": ["iss", "aud", "sub", "exp", "iat"]},
        )
    except jwt.ExpiredSignatureError as e:
        raise JWTExpired("token expired") from e
    except jwt.InvalidTokenError as e:
        raise JWTInvalid(str(e)) from e

    return TokenClaims(
        sub=str(payload["sub"]),
        aud=str(payload["aud"]),
        iss=str(payload["iss"]),
        exp=int(payload["exp"]),
        iat=int(payload["iat"]),
        jti=str(payload.get("jti", "")),
        email=payload.get("email"),
    )


def generate_secret() -> str:
    """Generate a strong JWT signing secret (256 bits of entropy)."""
    return secrets.token_urlsafe(32)
