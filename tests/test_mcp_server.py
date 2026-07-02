"""MCP server smoke tests — server boots, tools register, health responds."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from afair.mcp.context import clear_context
from afair.mcp.server import build_server
from afair.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolated_context(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test gets a clean module-level context and a no-op extractor.

    The MCP-protocol smoke tests exercise the tool registration + call path,
    not the LLM — extractor work is covered separately in test_extractor.py.
    """
    monkeypatch.setattr(
        "afair.mcp.handlers.schedule_extraction",
        lambda _event_id: None,
    )
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
        # Phase 3 cold-path workers race with the test's own DB connection
        # on SQLite write locks. Disable for the build_server tests; the
        # workers are tested directly in tests/test_phase3_workers.py.
        cold_path_enabled=False,
    )


@pytest.mark.asyncio
async def test_server_builds_and_registers_all_v1_tools(tmp_path: Path) -> None:
    """Server boots and exposes the v1 tool surface per Invariant I1.

    I1 is additive: tools are added forever; existing signatures never
    change. Pre-release collapse on 2026-05-26 fixed the surface at
    three verbs: remember (with invalidates kwarg), recall (with by_id,
    by_content_hash, full_payload, stats), observe. The old list_context,
    get_event, invalidate verbs were absorbed before any external user
    saw them. Per I1, this surface is now forever-stable.
    """
    server = build_server(_settings_for(tmp_path))
    tools = await server.list_tools()
    tool_names = {t.name for t in tools}
    assert tool_names == {"remember", "recall", "observe"}


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
    assert "Sajinth" in data["hits"][0]["payload"]["text"]
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


def _tool_data(result: object) -> dict:
    return result.data if hasattr(result, "data") else result.structured_content  # type: ignore[attr-defined,no-any-return]


# ── stringified-object params (write-first intake at the live call layer) ─────
#
# Regression coverage for the HIGH-severity data-loss bug: FastMCP validates
# tool args via ``TypeAdapter(fn).validate_python``. In that parameter (FieldInfo)
# context, ``Field(discriminator="type")`` was hoisted OUTSIDE the WrapValidator,
# so the write-first coercers never ran — a stringified ``content``/``event`` (and
# the bare-string / wrong-tag tolerances b9ba3fc added) were rejected or garbled
# BEFORE reaching the substrate. These tests exercise ``call_tool`` directly, the
# exact layer a live MCP client hits (the type-level tests in test_mcp_handlers.py
# passed while this path failed).


@pytest.mark.asyncio
async def test_remember_stringified_object_content_parsed(tmp_path: Path) -> None:
    """T1: content passed as a JSON string persists the REAL text, not the blob."""
    import json

    server = build_server(_settings_for(tmp_path))
    result = await server.call_tool(
        "remember",
        {"content": json.dumps({"type": "text", "text": "HOPE stringified content"})},
    )
    data = _tool_data(result)
    assert data["ok"] is True

    recall = await server.call_tool("recall", {"query": "HOPE", "full_payload": True, "limit": 5})
    hits = _tool_data(recall)["hits"]
    texts = [h["payload"].get("text") for h in hits]
    assert "HOPE stringified content" in texts
    # The raw JSON string must NOT have been stored as literal text.
    assert all(not (t or "").startswith('{"type"') for t in texts)


@pytest.mark.asyncio
async def test_observe_stringified_object_event_parsed(tmp_path: Path) -> None:
    """T2: event passed as a JSON string parses into action/subject/result."""
    import json

    server = build_server(_settings_for(tmp_path))
    result = await server.call_tool(
        "observe",
        {"event": json.dumps({"action": "edit", "subject": "x", "result": "ok"})},
    )
    data = _tool_data(result)
    assert data["ok"] is True

    recall = await server.call_tool("recall", {"by_id": data["event_id"], "full_payload": True})
    payload = _tool_data(recall)["hits"][0]["payload"]
    assert payload["action"] == "edit"
    assert payload["subject"] == "x"
    assert payload["result"] == "ok"
    # The whole blob must NOT have been garbled into ``action``.
    assert "action_full" not in payload


@pytest.mark.asyncio
async def test_remember_bare_string_content_becomes_text(tmp_path: Path) -> None:
    """T3: a bare non-JSON string still lands as a text event (b9ba3fc tolerance)."""
    server = build_server(_settings_for(tmp_path))
    result = await server.call_tool("remember", {"content": "a plain non-JSON sentence"})
    data = _tool_data(result)
    assert data["ok"] is True

    recall = await server.call_tool(
        "recall", {"query": "plain non-JSON", "full_payload": True, "limit": 5}
    )
    hits = _tool_data(recall)["hits"]
    assert any(h["payload"].get("text") == "a plain non-JSON sentence" for h in hits)


@pytest.mark.asyncio
async def test_remember_wrong_tag_dict_coerced_to_text(tmp_path: Path) -> None:
    """T4: a dict whose ``type`` isn't a content tag coerces to text, not rejected."""
    server = build_server(_settings_for(tmp_path))
    result = await server.call_tool(
        "remember",
        {"content": {"type": "fact", "text": "wrong-tag but salvageable"}},
    )
    data = _tool_data(result)
    assert data["ok"] is True

    recall = await server.call_tool(
        "recall", {"query": "salvageable", "full_payload": True, "limit": 5}
    )
    hits = _tool_data(recall)["hits"]
    assert any(h["payload"].get("text") == "wrong-tag but salvageable" for h in hits)


@pytest.mark.asyncio
async def test_remember_stringified_binary_stored_as_binary(tmp_path: Path) -> None:
    """T7: a stringified VALID binary object round-trips as a binary event.

    Proves post-parse union validation (not a blind text fallback): the parsed
    dict is a well-formed binary payload, so it must persist as ``binary``.
    """
    import base64
    import json

    server = build_server(_settings_for(tmp_path))
    data_b64 = base64.b64encode(b"\x00\x01\x02binary bytes").decode()
    result = await server.call_tool(
        "remember",
        {
            "content": json.dumps(
                {
                    "type": "binary",
                    "data_b64": data_b64,
                    "mime": "application/octet-stream",
                }
            )
        },
    )
    data = _tool_data(result)
    assert data["ok"] is True

    recall = await server.call_tool(
        "recall", {"by_content_hash": data["content_hash"], "full_payload": True}
    )
    payload = _tool_data(recall)["hits"][0]["payload"]
    assert payload["content_type"] == "binary"


@pytest.mark.asyncio
async def test_observe_bare_string_event_becomes_action(tmp_path: Path) -> None:
    """T5: a bare non-JSON event string still becomes the action (pin)."""
    server = build_server(_settings_for(tmp_path))
    result = await server.call_tool("observe", {"event": "just did a thing"})
    data = _tool_data(result)
    assert data["ok"] is True

    recall = await server.call_tool("recall", {"by_id": data["event_id"], "full_payload": True})
    payload = _tool_data(recall)["hits"][0]["payload"]
    assert payload["action"] == "just did a thing"


def _string_alt_present(node: dict) -> bool:
    """A plain-string member (no const tag, no object properties) is advertised."""
    members = node.get("anyOf", [node])
    return any(
        m.get("type") == "string" and "const" not in m and "properties" not in m for m in members
    )


def _discriminator_tags(node: dict) -> set[str]:
    tags: set[str] = set()

    def walk(m: dict) -> None:
        const = m.get("properties", {}).get("type", {})
        if "const" in const:
            tags.add(const["const"])
        for sub in m.get("oneOf", []) + m.get("anyOf", []):
            walk(sub)

    for m in node.get("anyOf", [node]):
        walk(m)
    return tags


@pytest.mark.asyncio
async def test_advertised_input_schema_is_i1_superset(tmp_path: Path) -> None:
    """T8: the advertised inputSchema is a strict SUPERSET of the prior contract.

    Every object variant that was valid before stays valid (all four remember
    content tags; the observe event object), and a string alternative is added.
    Locks the I1-additive guarantee so a future change can't silently drop the
    string acceptance or a content variant from the frozen surface.
    """
    server = build_server(_settings_for(tmp_path))
    tools = {t.name: t for t in await server.list_tools()}

    content = tools["remember"].parameters["properties"]["content"]
    assert _discriminator_tags(content) == {"text", "binary", "blob-ref", "compound"}
    assert _string_alt_present(content), "remember.content must advertise a string alt"

    event = tools["observe"].parameters["properties"]["event"]
    event_members = event.get("anyOf", [event])
    assert any(m.get("type") == "object" for m in event_members), (
        "observe.event must still advertise its object form"
    )
    assert _string_alt_present(event), "observe.event must advertise a string alt"


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
