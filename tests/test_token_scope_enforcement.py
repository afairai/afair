"""Token-scope enforcement at the write-verb boundary (Security L2).

A read-scoped minted token must not be able to call remember/observe.
``enforce_write_scope`` reads the authenticated credential's scope from
the current HTTP request's ASGI scope and raises for read-only tokens,
while failing open when there is no HTTP context (direct/in-process calls).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from afair.mcp import auth


class _FakeRequest:
    def __init__(self, token_scope: str | None) -> None:
        self.scope: dict = {}
        if token_scope is not None:
            self.scope[auth.SCOPE_TOKEN_SCOPE_KEY] = token_scope


def test_read_scope_blocks_write() -> None:
    with patch(
        "fastmcp.server.dependencies.get_http_request",
        return_value=_FakeRequest("read"),
    ):
        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError):
            auth.enforce_write_scope()


def test_write_scope_allows_write() -> None:
    with patch(
        "fastmcp.server.dependencies.get_http_request",
        return_value=_FakeRequest("write"),
    ):
        auth.enforce_write_scope()  # no raise


def test_full_scope_allows_write() -> None:
    with patch(
        "fastmcp.server.dependencies.get_http_request",
        return_value=_FakeRequest("full"),
    ):
        auth.enforce_write_scope()  # no raise


def test_missing_scope_defaults_to_full() -> None:
    # An authenticated request that somehow lacks the scope key (e.g. an
    # older code path) defaults to full — never accidentally locks a user out.
    with patch(
        "fastmcp.server.dependencies.get_http_request",
        return_value=_FakeRequest(None),
    ):
        auth.enforce_write_scope()  # no raise


def test_no_http_context_fails_open() -> None:
    # Direct/in-process invocation (unit tests, cold-path workers) has no
    # HTTP request — must be allowed.
    with patch(
        "fastmcp.server.dependencies.get_http_request",
        side_effect=RuntimeError("no request context"),
    ):
        auth.enforce_write_scope()  # no raise
