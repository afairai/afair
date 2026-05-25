"""OAuth discovery metadata endpoints.

Two endpoints, both RFC-standardized:
  - /.well-known/oauth-protected-resource  (RFC 9728) — tells MCP clients
    "this resource is OAuth-protected; here is its authorization server."
  - /.well-known/oauth-authorization-server (RFC 8414) — describes the
    authorization server itself (endpoints, supported grant types, PKCE, DCR).

Both are publicly readable (no auth) so clients can discover the auth
contract before they have any credentials.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...settings import Settings


def protected_resource_metadata(settings: Settings) -> dict[str, object]:
    """RFC 9728 — Protected Resource Metadata.

    Points clients at the authorization server they should authenticate
    against. Our own server is also the authorization server, so the
    pointer is self-referential.
    """
    issuer = settings.effective_oauth_issuer
    return {
        "resource": f"{issuer}/mcp",
        "authorization_servers": [issuer],
        "bearer_methods_supported": ["header"],
        "resource_documentation": f"{issuer}/",
        "scopes_supported": ["mcp"],
    }


def authorization_server_metadata(settings: Settings) -> dict[str, object]:
    """RFC 8414 — OAuth Authorization Server Metadata.

    The full OAuth 2.1 + PKCE + DCR contract our server speaks. MCP
    clients use this to discover the /authorize and /token endpoints
    and to determine whether DCR is available.
    """
    issuer = settings.effective_oauth_issuer
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/oauth/authorize",
        "token_endpoint": f"{issuer}/oauth/token",
        "registration_endpoint": f"{issuer}/oauth/register",
        "revocation_endpoint": f"{issuer}/oauth/revoke",
        "scopes_supported": ["mcp"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "code_challenge_methods_supported": ["S256"],
        # RFC 9207 — we include `iss` in authorization response redirects.
        # MCP 2026-07-28 RC requires clients to validate this; Claude.ai
        # rejects responses without it.
        "authorization_response_iss_parameter_supported": True,
        "service_documentation": f"{issuer}/",
    }
