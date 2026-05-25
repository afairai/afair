"""GitHub OAuth identity backend.

The user authenticates against GitHub; we receive back the GitHub
username + primary email. We never see their GitHub password.

Phase 1 default. Why GitHub:
  - Developers (initial audience) already have accounts
  - GitHub OAuth setup is 5 minutes
  - No email infrastructure needed (vs. magic-link)
  - Identity proof is rigorous (GitHub verified the email)
  - Self-hostable trivially — each operator registers their own GitHub
    OAuth app
"""

from __future__ import annotations

from typing import Any, ClassVar

import httpx
from httpx_oauth.clients.github import GitHubOAuth2

from .base import AuthenticatedUser, IdentityBackend


class GitHubIdentityBackend(IdentityBackend):
    """OAuth-based identity backend backed by GitHub.

    We request the ``user:email`` scope which gives access to the
    /user and /user/emails endpoints. No write scopes — GitHub is
    identity only, never used to push commits or modify resources.
    """

    SCOPES: ClassVar[list[str]] = ["read:user", "user:email"]

    def __init__(self, *, client_id: str, client_secret: str) -> None:
        if not client_id or not client_secret:
            msg = "GitHub OAuth client_id and client_secret are required"
            raise ValueError(msg)
        self._client = GitHubOAuth2(client_id, client_secret, scopes=self.SCOPES)

    async def authorize_url(self, *, state: str, redirect_uri: str) -> str:
        return await self._client.get_authorization_url(redirect_uri, state=state)

    async def fetch_user(self, *, code: str, redirect_uri: str) -> AuthenticatedUser:
        token = await self._client.get_access_token(code, redirect_uri)
        access_token = token["access_token"]

        async with httpx.AsyncClient(timeout=10.0) as http:
            user_response = await http.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            user_response.raise_for_status()
            user_data: dict[str, Any] = user_response.json()

            email = user_data.get("email")
            # Public profile email is sometimes null even when emails exist.
            # Fetch /user/emails to find the primary verified one.
            if not email:
                emails_response = await http.get(
                    "https://api.github.com/user/emails",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                )
                if emails_response.status_code == 200:
                    for entry in emails_response.json():
                        if entry.get("primary") and entry.get("verified"):
                            email = entry.get("email")
                            break

        username = user_data.get("login")
        if not isinstance(username, str) or not username:
            msg = "GitHub returned no `login` (username) for the user"
            raise RuntimeError(msg)

        return AuthenticatedUser(
            sub=username,
            email=email if isinstance(email, str) else None,
            raw=user_data,
        )
