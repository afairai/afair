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
    def __init__(self, token_scope: str | None, identity: str | None = None) -> None:
        self.scope: dict = {}
        if token_scope is not None:
            self.scope[auth.SCOPE_TOKEN_SCOPE_KEY] = token_scope
        if identity is not None:
            self.scope[auth.SCOPE_IDENTITY_KEY] = identity


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


def test_no_auth_local_mode_allows_write() -> None:
    # Local self-host / no-auth mode: the middleware stamps NEITHER the
    # identity nor the scope key. Writes must stay allowed (the hero path).
    with patch(
        "fastmcp.server.dependencies.get_http_request",
        return_value=_FakeRequest(None),
    ):
        auth.enforce_write_scope()  # no raise


def test_authenticated_without_scope_fails_closed() -> None:
    # Anomalous: auth ran (identity stamped) but no token scope was
    # recorded. Every successful auth path stamps both keys, so this
    # should be impossible — deny writes rather than fail open.
    with patch(
        "fastmcp.server.dependencies.get_http_request",
        return_value=_FakeRequest(None, identity="api-token:abc"),
    ):
        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError):
            auth.enforce_write_scope()


def test_no_http_context_fails_open() -> None:
    # Direct/in-process invocation (unit tests, cold-path workers) has no
    # HTTP request — must be allowed.
    with patch(
        "fastmcp.server.dependencies.get_http_request",
        side_effect=RuntimeError("no request context"),
    ):
        auth.enforce_write_scope()  # no raise


# ── recall(decide=) is a write and must be scope-gated at the tool layer ────


@pytest.fixture()
def _mcp_server(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """A built MCP server with isolated context (mirrors test_mcp_server.py)."""
    from afair.mcp.context import clear_context
    from afair.mcp.server import build_server
    from afair.settings import Settings

    monkeypatch.setattr(
        "afair.mcp.handlers.schedule_extraction",
        lambda _event_id: None,
    )
    clear_context()
    try:
        yield build_server(
            Settings(
                _env_file=None,  # type: ignore[call-arg]
                environment="local",
                vault_dir=tmp_path,
                cold_path_enabled=False,
            )
        )
    finally:
        clear_context()


@pytest.mark.asyncio
async def test_recall_decide_blocked_for_read_scope(_mcp_server) -> None:  # type: ignore[no-untyped-def]
    # recall(decide=...) applies corrections (entity merges, retractions,
    # observe events) — a read-scoped token must not be able to do that.
    from fastmcp.exceptions import ToolError

    with (
        patch(
            "fastmcp.server.dependencies.get_http_request",
            return_value=_FakeRequest("read"),
        ),
        pytest.raises(ToolError, match="read-only scope"),
    ):
        await _mcp_server.call_tool(
            "recall",
            {"decide": {"proposal_id": "prop-1", "verdict": "confirm"}},
        )


@pytest.mark.asyncio
async def test_recall_without_decide_allowed_for_read_scope(_mcp_server) -> None:  # type: ignore[no-untyped-def]
    # Plain recall is a read — read-scoped tokens must keep working.
    with patch(
        "fastmcp.server.dependencies.get_http_request",
        return_value=_FakeRequest("read"),
    ):
        result = await _mcp_server.call_tool("recall", {"query": "anything"})
    data = result.data if hasattr(result, "data") else result.structured_content
    assert data["hits"] == []
