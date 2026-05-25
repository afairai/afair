"""OAuth 2.1 authorization server for the MCP surface.

We are our own IdP. Identity (who is this user?) comes from a pluggable
backend (Phase 1: GitHub OAuth). Tokens are JWTs signed with our HS256
secret. The MCP server's auth middleware accepts EITHER the existing
static bearer token (defense in depth) OR a valid JWT we issued.

Per VISION.md §6.7 the orchestration layer (Phase 8) will be the same
OAuth server, deployed centrally — that's why we build it ourselves rather
than depending on a managed IdP. No vendor lock; ships unchanged for self-
host (I4).
"""

from __future__ import annotations
