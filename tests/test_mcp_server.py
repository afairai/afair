"""MCP server smoke tests — server boots, tools register, health responds."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from neverforget.mcp.context import clear_context
from neverforget.mcp.server import build_server
from neverforget.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolated_context() -> Iterator[None]:
    """Each test gets a clean module-level context."""
    clear_context()
    try:
        yield
    finally:
        clear_context()


def _settings_for(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=tmp_path,
    )


@pytest.mark.asyncio
async def test_server_builds_and_registers_all_four_tools(tmp_path: Path) -> None:
    """Server boots and exposes exactly the four v1 tools per Invariant I1."""
    server = build_server(_settings_for(tmp_path))
    tools = await server.list_tools()
    tool_names = {t.name for t in tools}
    assert tool_names == {"remember", "recall", "list_context", "observe"}


@pytest.mark.asyncio
async def test_tool_descriptions_are_meaningful(tmp_path: Path) -> None:
    """Tool descriptions must be substantive — they ARE the AI-facing prompt."""
    server = build_server(_settings_for(tmp_path))
    tools = await server.list_tools()
    for t in tools:
        assert t.description is not None
        # Sanity floor: descriptions should be at least a few hundred chars
        assert len(t.description) > 200, (
            f"tool {t.name} description is too thin ({len(t.description)} chars)"
        )
        # Each description must tell the AI WHEN to call
        assert "WHEN TO CALL" in t.description, (
            f"tool {t.name} description lacks WHEN-TO-CALL guidance"
        )


@pytest.mark.asyncio
async def test_remember_via_mcp_protocol(tmp_path: Path) -> None:
    """End-to-end: call remember through the MCP server's call_tool path."""
    server = build_server(_settings_for(tmp_path))
    result = await server.call_tool(
        "remember",
        {
            "content": {"type": "text", "text": "Sajinth proposed a new roadmap"},
            "context": "email",
        },
    )
    # FastMCP returns a structured result with .data or .structured_content
    data = result.data if hasattr(result, "data") else result.structured_content
    assert data["ok"] is True
    assert data["content_hash"].startswith("sha256:")


@pytest.mark.asyncio
async def test_recall_via_mcp_protocol(tmp_path: Path) -> None:
    """Write via remember, read via recall, through MCP."""
    server = build_server(_settings_for(tmp_path))
    await server.call_tool(
        "remember",
        {
            "content": {
                "type": "text",
                "text": "Sajinth proposed a new roadmap focused on memory",
            },
            "context": "email",
        },
    )
    result = await server.call_tool("recall", {"query": "Sajinth"})
    data = result.data if hasattr(result, "data") else result.structured_content
    assert len(data["hits"]) == 1
    assert "Sajinth" in data["hits"][0]["payload_summary"]["text"]
    assert data["depth_used"] == "shallow"


@pytest.mark.asyncio
async def test_observe_via_mcp_protocol(tmp_path: Path) -> None:
    server = build_server(_settings_for(tmp_path))
    result = await server.call_tool(
        "observe",
        {
            "event": {
                "action": "edit_file",
                "subject": "events.py",
                "result": "added inline logic",
            }
        },
    )
    data = result.data if hasattr(result, "data") else result.structured_content
    assert data["ok"] is True


@pytest.mark.asyncio
async def test_health_endpoint_returns_ok(tmp_path: Path) -> None:
    """The /health route returns 200 OK when the substrate is healthy."""
    from starlette.testclient import TestClient

    server = build_server(_settings_for(tmp_path))
    app = server.http_app()
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
