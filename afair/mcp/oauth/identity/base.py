"""The pluggable-identity contract.

Every backend implements two methods:
  - ``authorize_url(state, redirect_uri)`` — where to send the user's browser
  - ``fetch_user(code, redirect_uri)``     — exchange the backend's code
                                              for the authenticated identity

Backend implementations should keep the surface tight — no leaky internal
state. The OAuth server only sees ``AuthenticatedUser`` after the dance
completes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AuthenticatedUser:
    """The result of a successful identity dance.

    Attributes:
      sub:   The canonical identity string for this user (e.g., GitHub
             username). Used as the JWT ``sub`` claim AND for allowlist
             comparison. Case-insensitive comparison.
      email: Best-effort primary email. Optional; may be ``None``.
      raw:   The backend's raw user payload, preserved verbatim. Useful
             for richer per-backend claims later without touching this
             contract.
    """

    sub: str
    email: str | None
    raw: dict[str, Any] = field(default_factory=dict)


class IdentityBackend(ABC):
    """Pluggable identity provider. One per deployment."""

    @abstractmethod
    async def authorize_url(self, *, state: str, redirect_uri: str) -> str:
        """Build the URL to redirect the user-agent to.

        ``state`` is opaque to the backend — we generate it and store
        it in oauth_login_state; on callback we use it to retrieve the
        original /authorize request context.
        ``redirect_uri`` is OUR callback URL on this server, NOT the
        client's redirect URI.
        """

    @abstractmethod
    async def fetch_user(self, *, code: str, redirect_uri: str) -> AuthenticatedUser:
        """Exchange the backend's auth code for the authenticated identity.

        Called from our identity callback handler. Raises on any failure
        (the caller maps to a 4xx OAuth error response).
        """
