#!/usr/bin/env python
"""Full MCP-protocol smoke test against the deployed server.

Uses FastMCP's client library to connect, list tools, exercise a tiny
remember + recall round-trip, and verify the four v1 tool signatures
match the I1 contract.

Run via:
    uv run python scripts/smoke_mcp.py
    URL=... TOKEN=... uv run python scripts/smoke_mcp.py

Exits 0 on success, 1 on any failure. Logs structured JSON to stdout.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport


def _extract(result: Any) -> dict[str, Any]:
    """Extract a dict from a FastMCP CallToolResult, however it's shaped."""
    if hasattr(result, "structured_content") and result.structured_content:
        return result.structured_content  # type: ignore[return-value]
    if hasattr(result, "data"):
        data = result.data
        if hasattr(data, "model_dump"):
            return data.model_dump()
        if isinstance(data, dict):
            return data
    if isinstance(result, dict):
        return result
    return {"_raw": str(result)}


def _load_token() -> str:
    token = os.environ.get("TOKEN")
    if token:
        return token
    env_local = Path(".env.local")
    if not env_local.exists():
        msg = "TOKEN env var unset and .env.local not found"
        raise SystemExit(msg)
    for line in env_local.read_text().splitlines():
        if line.startswith("NEVERFORGET_AUTH_TOKEN="):
            return line.split("=", 1)[1].strip()
    msg = "NEVERFORGET_AUTH_TOKEN not in .env.local"
    raise SystemExit(msg)


def _expected_tool_names() -> set[str]:
    return {"remember", "recall", "list_context", "observe"}


async def main() -> int:
    url = os.environ.get("URL", "https://neverforget.fly.dev")
    token = _load_token()
    # FastMCP serves at /mcp (no trailing slash). A trailing-slash request
    # triggers a 307 redirect which httpx (under the MCP client) strips the
    # Authorization header from — so we MUST hit the canonical path directly.
    endpoint = url.rstrip("/") + "/mcp"
    checks: list[tuple[str, bool, str]] = []

    transport = StreamableHttpTransport(
        url=endpoint,
        headers={"Authorization": f"Bearer {token}"},
    )

    async with Client(transport) as client:
        # 1. tools/list contract — exactly the four v1 tools
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}
        expected = _expected_tool_names()
        checks.append(
            (
                "tools/list returns exactly the four v1 tools (I1 contract)",
                tool_names == expected,
                f"got {sorted(tool_names)}, expected {sorted(expected)}",
            )
        )

        # 2. each tool description contains WHEN TO CALL guidance
        for t in tools:
            description = t.description or ""
            checks.append(
                (
                    f"  {t.name} description has WHEN-TO-CALL guidance",
                    "WHEN TO CALL" in description,
                    f"description length: {len(description)}",
                )
            )

        # 3. round-trip remember → recall on the live substrate
        marker = f"smoke-test-{int(time.time())}"
        remember_text = (
            f"This is a Phase 0 cross-vendor smoke marker {marker}. "
            "If recall finds this, the substrate round-trip is working."
        )

        remember_result = await client.call_tool(
            "remember",
            {
                "content": {"type": "text", "text": remember_text},
                "context": "scripts/smoke_mcp.py — automated smoke",
                "type_hint": "smoke_test_marker",
            },
        )
        remember_data = _extract(remember_result)
        checks.append(
            (
                "remember(text) returns ok=true",
                bool(remember_data.get("ok")),
                f"got {remember_data}",
            )
        )
        checks.append(
            (
                "remember returns sha256-prefixed content_hash",
                str(remember_data.get("content_hash", "")).startswith("sha256:"),
                f"got {remember_data.get('content_hash')}",
            )
        )

        # Short delay to let the warm-path Extractor settle (its result
        # isn't required by recall, which uses FTS5 — but the delay also
        # gives Fly's volume time to flush).
        await asyncio.sleep(0.5)

        recall_result = await client.call_tool(
            "recall",
            {"query": marker},
        )
        recall_data = _extract(recall_result)
        hits = recall_data.get("hits", [])
        checks.append(
            (
                "recall finds the just-remembered marker",
                len(hits) >= 1 and any(marker in str(h) for h in hits),
                f"got {len(hits)} hits",
            )
        )
        checks.append(
            (
                "recall reports depth_used in {shallow, normal}",
                recall_data.get("depth_used") in {"shallow", "normal"},
                f"got {recall_data.get('depth_used')}",
            )
        )

        # 4. observe — agent-self-log
        observe_result = await client.call_tool(
            "observe",
            {
                "event": {
                    "action": "smoke_test_run",
                    "subject": "scripts/smoke_mcp.py",
                    "result": "verified",
                    "marker": marker,
                }
            },
        )
        observe_data = _extract(observe_result)
        checks.append(
            (
                "observe(event) returns ok=true",
                bool(observe_data.get("ok")),
                f"got {observe_data}",
            )
        )

        # 5. list_context — survey
        list_result = await client.call_tool("list_context", {"limit": 5})
        list_data = _extract(list_result)
        total = list_data.get("summary", {}).get("total_events", 0)
        checks.append(
            (
                "list_context reports total_events >= 2 (remember + observe just landed)",
                total >= 2,
                f"got total_events={total}",
            )
        )

    # ── report
    fail_count = 0
    for name, ok, detail in checks:
        glyph = "\033[32m✓\033[0m" if ok else "\033[31m✗\033[0m"
        print(f"  {glyph}  {name}")
        if not ok:
            print(f"        {detail}")
            fail_count += 1

    print()
    print(f"  passed: {len(checks) - fail_count}/{len(checks)}")
    if fail_count > 0:
        print("  status: FAILED")
        return 1
    print("  status: HEALTHY — substrate round-trip works")
    print()
    print(f"  marker written: {marker!r}")
    print("  (this fact is now durable in your vault, findable via recall)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
