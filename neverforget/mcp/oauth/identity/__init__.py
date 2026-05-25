"""Identity backends — pluggable per deployment.

The OAuth resource server (us) delegates "who is this user?" to a backend.
Phase 1 ships GitHub OAuth as the default. Later phases add:
  - magic-link (email-based, for non-GitHub users)
  - clerk (managed convenience for the SaaS-hosted variant)
  - static-password (dev/lab only)

A backend implements the small contract in ``base.IdentityBackend``.
The OAuth server's /authorize flow calls ``authorize_url()`` to send the
user to the backend; the backend's callback hits ``fetch_user()`` to get
the authenticated identity back.
"""

from __future__ import annotations

from .base import AuthenticatedUser, IdentityBackend
from .github import GitHubIdentityBackend

__all__ = ["AuthenticatedUser", "GitHubIdentityBackend", "IdentityBackend"]
