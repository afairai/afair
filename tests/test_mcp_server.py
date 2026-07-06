"""MCP server smoke tests — server boots, tools register, health responds."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from afair.mcp import schemas
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


def test_thread_limiter_cap_applies_in_event_loop(tmp_path: Path) -> None:
    """P2a: _apply_thread_limiter_cap sets anyio's default thread limiter to
    settings.max_tool_threads. Must run inside a running loop (the limiter is a
    per-loop RunVar) — the app lifespan is exactly that context."""
    import anyio

    from afair.mcp.server import _apply_thread_limiter_cap

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=tmp_path,
        max_tool_threads=7,
    )

    async def _run() -> int:
        _apply_thread_limiter_cap(settings)
        return anyio.to_thread.current_default_thread_limiter().total_tokens

    assert anyio.run(_run) == 7


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


def _has_no_none(obj: object) -> bool:
    """Recursively assert no None appears anywhere in a structured value."""
    if obj is None:
        return False
    if isinstance(obj, dict):
        return all(_has_no_none(v) for v in obj.values())
    if isinstance(obj, list):
        return all(_has_no_none(v) for v in obj)
    return True


@pytest.mark.asyncio
async def test_recall_serialization_is_null_free_with_output_schema(tmp_path: Path) -> None:
    """P1-2 §4.3: recall returns a ToolResult whose structuredContent + text
    body are both null-free (exclude_none), and the advertised output_schema is
    preserved despite the ToolResult return annotation."""
    import json

    from fastmcp.tools import ToolResult

    from afair.mcp.schemas import RecallResult

    server = build_server(_settings_for(tmp_path))
    await server.call_tool(
        "remember",
        {"content": {"type": "text", "text": "Sajinth proposed a roadmap"}, "context": "email"},
    )
    result = await server.call_tool("recall", {"query": "Sajinth"})
    assert isinstance(result, ToolResult)

    sc = result.structured_content
    assert sc is not None
    assert _has_no_none(sc)  # no None anywhere in the structured payload

    # The TextContent body parses back to exactly the structured content.
    text_items = [c for c in result.content if getattr(c, "type", None) == "text"]
    assert text_items
    assert json.loads(text_items[0].text) == sc

    # The advertised outputSchema survives the ToolResult return annotation.
    tools = await server.list_tools()
    recall_tool = next(t for t in tools if t.name == "recall")
    assert recall_tool.output_schema is not None
    schema = RecallResult.model_json_schema()
    assert recall_tool.output_schema.get("title") == schema["title"]
    assert set(recall_tool.output_schema["properties"]) == set(schema["properties"])
    assert "next_cursor" in recall_tool.output_schema["properties"]
    assert "decisions" in recall_tool.output_schema["properties"]


@pytest.mark.asyncio
async def test_remember_observe_results_unchanged(tmp_path: Path) -> None:
    """remember/observe still return their plain result models (no ToolResult
    wrapper) — only recall changed shape."""
    server = build_server(_settings_for(tmp_path))
    r = await server.call_tool(
        "remember", {"content": {"type": "text", "text": "x"}, "context": "c"}
    )
    assert _tool_data(r)["ok"] is True
    o = await server.call_tool("observe", {"event": {"action": "edit_file"}})
    assert _tool_data(o)["ok"] is True


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


@pytest.mark.asyncio
async def test_recall_decide_single_object_as_string_matches_native(tmp_path: Path) -> None:
    """T9: a JSON-stringified single ``decide`` produces the IDENTICAL outcome to
    the native object form (the stringified-param class, extended to decide).

    A non-existent proposal id yields a deterministic ``not_found`` outcome, so
    the decide was parsed + dispatched (not silently dropped)."""
    import json

    server = build_server(_settings_for(tmp_path))
    decision = {"proposal_id": "does-not-exist", "verdict": "confirm"}

    native = _tool_data(await server.call_tool("recall", {"decide": decision}))["decisions"]
    stringed = _tool_data(await server.call_tool("recall", {"decide": json.dumps(decision)}))[
        "decisions"
    ]

    assert len(stringed) == 1
    assert stringed[0]["proposal_id"] == "does-not-exist"
    assert stringed[0]["status"] == "not_found"
    assert stringed == native


@pytest.mark.asyncio
async def test_recall_decide_list_as_string_matches_native(tmp_path: Path) -> None:
    """T10: a JSON-stringified ``decide`` LIST produces the identical batch
    outcome to the native list form."""
    import json

    server = build_server(_settings_for(tmp_path))
    batch = [
        {"proposal_id": "missing-a", "verdict": "confirm"},
        {"proposal_id": "missing-b", "verdict": "reject"},
    ]

    native = _tool_data(await server.call_tool("recall", {"decide": batch}))["decisions"]
    stringed = _tool_data(await server.call_tool("recall", {"decide": json.dumps(batch)}))[
        "decisions"
    ]

    assert len(stringed) == 2
    assert {d["proposal_id"] for d in stringed} == {"missing-a", "missing-b"}
    assert stringed == native


@pytest.mark.asyncio
async def test_recall_decide_malformed_json_string_errors_not_dropped(tmp_path: Path) -> None:
    """T11: a malformed ``decide`` JSON string yields a clear typed error rather
    than being silently dropped (which would discard the operator's correction).
    """
    from pydantic import ValidationError

    # ensure_decide is the narrowing used in the recall body; a malformed JSON
    # string reaches the union validator and raises, never returns a no-op.
    with pytest.raises(ValidationError):
        schemas.ensure_decide("{not valid json")


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
async def test_advertised_input_schema_is_clean_no_string_pollution(tmp_path: Path) -> None:
    """T8: the advertised inputSchema is the CLEAN pre-v0.1.9 shape — NO spurious
    top-level ``{"type":"string"}`` alternative on ``remember.content``,
    ``observe.event``, or ``recall.decide``.

    Regression lock for a user-blocking bug: the v0.1.9 stringified-param fix
    widened these params to ``<X> | str``, which leaked a top-level
    ``anyOf: [<object/union>, {type:string}]`` into the advertised schema and
    broke claude.ai's in-chat tool surfacing (it stopped handing the connector's
    tools to the assistant). String tolerance is now a coercion, not a schema
    member — so the object contract stays clean while stringified payloads still
    parse (asserted by the behavioral tests above). This test would have caught
    the regression.
    """
    server = build_server(_settings_for(tmp_path))
    tools = {t.name: t for t in await server.list_tools()}

    # remember.content — all four content variants still advertised, NO string alt.
    content = tools["remember"].parameters["properties"]["content"]
    assert _discriminator_tags(content) == {"text", "binary", "blob-ref", "compound"}
    assert not _string_alt_present(content), (
        "remember.content must NOT advertise a top-level string alternative"
    )

    # observe.event — object form still advertised, NO string alt.
    event = tools["observe"].parameters["properties"]["event"]
    event_members = event.get("anyOf", [event])
    assert any(m.get("type") == "object" for m in event_members), (
        "observe.event must still advertise its object form"
    )
    assert not _string_alt_present(event), (
        "observe.event must NOT advertise a top-level string alternative"
    )

    # recall.decide — the intrinsic CorrectionDecision | list | None union, NO
    # string alt (a string decide is coerced, not advertised).
    decide = tools["recall"].parameters["properties"]["decide"]
    assert not _string_alt_present(decide), (
        "recall.decide must NOT advertise a top-level string alternative"
    )


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


# ── Phase 0.5 observability enrichment ───────────────────────────────────────


def _health(tmp_path: Path) -> dict:
    """Build the server and GET /health, returning the parsed body."""
    from starlette.testclient import TestClient

    server = build_server(_settings_for(tmp_path))
    app = server.http_app()
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    return response.json()  # type: ignore[no-any-return]


@pytest.mark.asyncio
async def test_health_includes_version_and_pipeline_block(tmp_path: Path) -> None:
    """A seeded snapshot surfaces in /health with the app version and the
    counts it stored (counts only — no content)."""
    import afair
    from afair.substrate import observability, open_db

    conn = open_db(tmp_path)
    try:
        observability.write_snapshot(
            conn,
            producer="expectation_checker",
            counters={
                "stuck_extractions": 1,
                "pending_extraction_backlog": 2,
                "retry_exhausted": 2,
                "permanent_failures": 4,
                "expectation_violations": 3,
                "oldest_stuck_age_seconds": 5400,
                "lookback_days": 7,
            },
        )
    finally:
        conn.close()

    body = _health(tmp_path)
    assert body["status"] == "ok"
    assert body["version"] == afair.__version__
    assert body["checks"] == {"db": True}
    pipeline = body["pipeline"]
    assert pipeline["stuck_extractions"] == 1
    assert pipeline["pending_extraction_backlog"] == 2
    assert pipeline["retry_exhausted"] == 2
    assert pipeline["permanent_failures"] == 4
    assert pipeline["expectation_violations"] == 3
    assert pipeline["pipeline_ok"] is False
    assert pipeline["oldest_stuck_age_seconds"] == 5400
    assert isinstance(pipeline["snapshot_age_seconds"], int)


@pytest.mark.asyncio
async def test_health_end_to_end_surfaces_silent_failure(tmp_path: Path) -> None:
    """THE proof: a backdated event.written with no terminal extraction
    stage → the checker flags it → /health surfaces stuck_extractions >= 1
    and pipeline_ok False, while status stays "ok" / 200 (a backlog is not
    an unhealthy machine — decision §2C)."""
    from datetime import UTC, datetime, timedelta

    from ulid import ULID

    from afair.agents.expectation_checker import ExpectationChecker
    from afair.settings import Settings
    from afair.substrate import open_db
    from afair.substrate import pipeline_events as pe

    conn = open_db(tmp_path)
    try:
        written_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        with conn:
            conn.execute(
                """
                INSERT INTO pipeline_events
                    (id, event_id, event_hash, stage, status, recorded_at, producer, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(ULID()),
                    "01SILENT",
                    None,
                    pe.STAGE_EVENT_WRITTEN,
                    "ok",
                    written_at,
                    None,
                    None,
                ),
            )
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            environment="local",
            vault_dir=tmp_path,
        )
        stats = ExpectationChecker().run(conn, settings)
        assert stats["stuck_extractions"] == 1
    finally:
        conn.close()

    body = _health(tmp_path)
    assert body["status"] == "ok"  # still 200 — silent failure is visible, not fatal
    assert body["pipeline"]["stuck_extractions"] >= 1
    assert body["pipeline"]["pipeline_ok"] is False


@pytest.mark.asyncio
async def test_health_no_snapshot_yet(tmp_path: Path) -> None:
    """Before the checker's first cycle (or with cold path disabled), the
    pipeline block is null — a valid 200 body for self-hosters."""
    body = _health(tmp_path)
    assert body["status"] == "ok"
    assert body["pipeline"] is None
    assert body["workers"] is None


@pytest.mark.asyncio
async def test_health_body_contains_no_paths_or_content(tmp_path: Path) -> None:
    """Regression for the security rule: the serialized /health body must
    never contain the vault path or any seeded event content."""
    import json

    from ulid import ULID

    from afair.substrate import observability, open_db
    from afair.substrate import pipeline_events as pe

    secret_text = "SENSITIVE-PAYLOAD-DO-NOT-LEAK"
    conn = open_db(tmp_path)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO pipeline_events
                    (id, event_id, event_hash, stage, status, recorded_at, producer, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(ULID()),
                    "01LEAK",
                    None,
                    pe.STAGE_EVENT_WRITTEN,
                    "ok",
                    "2020-01-01T00:00:00+00:00",
                    secret_text,
                    secret_text,
                ),
            )
        observability.write_snapshot(
            conn, producer="expectation_checker", counters={"expectation_violations": 0}
        )
    finally:
        conn.close()

    body = _health(tmp_path)
    serialized = json.dumps(body)
    assert secret_text not in serialized
    assert str(tmp_path) not in serialized


@pytest.mark.asyncio
async def test_health_degraded_body_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the DB read fails, /health returns exactly {"status":
    "degraded"} with 503 and NO enrichment fields — the fleet's
    orchestrator keys off this byte-for-byte."""
    from starlette.testclient import TestClient

    server = build_server(_settings_for(tmp_path))

    def _boom() -> object:
        raise RuntimeError("unable to open /data/vault/substrate.db")

    monkeypatch.setattr("afair.mcp.server.connect_for_thread", _boom)

    app = server.http_app()
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "degraded"}
