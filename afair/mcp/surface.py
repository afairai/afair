"""Advertised MCP surface — the client-facing WIRE contract.

The three frozen v1 verbs (remember / recall / observe) are only useful if a
real MCP client can *see* and *call* them. What a client sees is not the
internal ``FastMCP.list_tools()`` view — it's the schema that travels over the
wire after ``initialize`` → ``tools/list``. FastMCP/pydantic transform the
internal annotations on the way out (discriminator hoisting, ``$defs``
inlining, union widening), and that transformed shape is what claude.ai's
assistant actually consumes.

The v0.1.9 incident proved the gap: a ``content | str`` widening leaked a
top-level ``anyOf: [<object>, {"type": "string"}]`` into the advertised
``inputSchema``. claude.ai listed the connector's tools in settings but stopped
handing them to the in-chat assistant. All 1168 tests stayed green because
nothing tested the *advertised* contract — only the internal one.

``advertised_surface`` captures exactly what a client sees, via the fastmcp
in-memory ``Client`` (a real ``initialize`` → ``tools/list`` round-trip), and
normalizes it into a byte-stable dict so a golden snapshot can lock it. Any
client-visible change — schema shape, description, tool name, outputSchema,
resource — becomes a red golden diff that forces a conscious human decision
(Invariant I1: the MCP surface is versioned and additive; shipped signatures
never break).

Determinism contract (must hold across processes and across py3.12/3.13):
  - tools sorted by name, resources sorted by uri;
  - ``meta.fastmcp`` reduced to the single I1 ``version`` marker (the rest is
    fastmcp bookkeeping that leaks internal state);
  - fastmcp's ``serverInfo.version`` (which is the fastmcp package version, not
    ours) excluded entirely — a framework bump must not silently rewrite our
    golden through that field;
  - server instructions captured as a sha256 (they are long, and a hash still
    diffs red on any change) — descriptions stay inline because they ARE the
    AI-facing prompt and a reviewer must see wording changes;
  - object keys sorted by ``canonical_json`` (``sort_keys=True``); array element
    order is left as pydantic emits it (deterministic from the type
    definitions).
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from fastmcp import Client

if TYPE_CHECKING:
    from fastmcp import FastMCP

# The single fastmcp ``meta`` key we keep: the per-tool I1 version marker.
# Everything else fastmcp stashes under ``meta.fastmcp`` (``versions``, ``tags``)
# is internal bookkeeping that would make the golden churn on framework bumps.
_I1_VERSION_KEY = "version"


def _normalize_meta(meta: dict[str, Any] | None) -> dict[str, Any]:
    """Reduce a tool's ``meta`` to just the I1 fastmcp version marker."""
    fastmcp_meta = (meta or {}).get("fastmcp") or {}
    version = fastmcp_meta.get(_I1_VERSION_KEY)
    if version is None:
        return {}
    return {"fastmcp": {_I1_VERSION_KEY: version}}


def _normalize_tool(dumped: dict[str, Any]) -> dict[str, Any]:
    """Capture the client-facing fields of one advertised tool.

    Only the fields a client acts on: name, title, the AI-facing description
    (inline), the wire in/out schemas, annotations, and the normalized I1 meta.
    fastmcp-internal fields (``execution``, ``icons``) are dropped so a
    framework bump can't churn the golden through a field we never contract on.
    """
    return {
        "name": dumped["name"],
        "title": dumped.get("title"),
        "description": dumped.get("description"),
        "inputSchema": dumped.get("inputSchema"),
        "outputSchema": dumped.get("outputSchema"),
        "annotations": dumped.get("annotations"),
        "meta": _normalize_meta(dumped.get("meta")),
    }


def _normalize_resource(dumped: dict[str, Any]) -> dict[str, Any]:
    """Capture the client-facing fields of one advertised resource.

    ``meta`` is dropped entirely (fastmcp stashes ``tags`` there) — a resource's
    contract to a client is its uri/name/mimeType/description.
    """
    return {
        "uri": str(dumped.get("uri")) if dumped.get("uri") is not None else None,
        "name": dumped.get("name"),
        "mimeType": dumped.get("mimeType"),
        "description": dumped.get("description"),
    }


async def advertised_surface(server: FastMCP) -> dict[str, Any]:
    """Return the normalized, byte-stable advertised MCP surface for ``server``.

    Uses the fastmcp in-memory ``Client`` so the captured schemas are the real
    WIRE form (post-``initialize`` ``tools/list`` / ``resources/list``), not the
    internal ``FastMCP.list_tools()`` view. See module docstring for the
    determinism contract.
    """
    async with Client(server) as client:
        init = client.initialize_result
        server_info = getattr(init, "serverInfo", None)
        server_name = getattr(server_info, "name", None) if server_info is not None else None
        instructions = getattr(init, "instructions", None) or ""

        tools = await client.list_tools()
        resources = await client.list_resources()

    normalized_tools = sorted(
        (_normalize_tool(t.model_dump(mode="json")) for t in tools),
        key=lambda t: t["name"],
    )
    normalized_resources = sorted(
        (_normalize_resource(r.model_dump(mode="json")) for r in resources),
        key=lambda r: r["uri"] or "",
    )

    return {
        "server": {"name": server_name},
        "instructions_sha256": hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
        "tools": normalized_tools,
        "resources": normalized_resources,
    }


def canonical_json(surface: dict[str, Any]) -> str:
    """Serialize a surface dict to the canonical, byte-stable golden form.

    Trailing newline so the committed golden is a well-formed text file and
    ``git diff`` renders the final line cleanly.
    """
    return json.dumps(surface, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
