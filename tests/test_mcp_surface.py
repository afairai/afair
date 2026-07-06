"""MCP surface compatibility guard — the advertised WIRE contract.

Golden snapshot + schema-shape lints over the surface a real MCP client sees
after ``initialize`` → ``tools/list`` (via the fastmcp in-memory ``Client``),
NOT the internal ``FastMCP.list_tools()`` view.

Why this file exists: v0.1.9 widened ``remember.content`` to ``content | str``,
which leaked a top-level ``anyOf: [<object>, {"type": "string"}]`` into the
advertised ``inputSchema`` and broke claude.ai's in-chat tool surfacing — while
all 1168 tests stayed green, because nothing tested the *advertised* contract.
This guard closes that gap:

  1. golden diff — any client-visible change (schema, description, name,
     outputSchema, resource) turns red and forces a conscious I1 decision;
  2. shape lints over EVERY param of EVERY tool — survive a careless
     re-blessing of the golden:
       - top-level inputSchema is ``type: object`` with ``properties`` and no
         top-level ``anyOf``/``oneOf``/``$ref``;
       - no bare-string pollution (the generalized v0.1.9 signature): a
         ``{"type": "string"}`` union member with no const/enum/format/pattern/
         maxLength — sitting in the same union as an object member — is
         forbidden. A legit ``str | None`` (string + null, no object co-member)
         passes;
       - every in/out schema is valid JSON Schema draft 2020-12.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from jsonschema import Draft202012Validator

from afair.mcp.context import clear_context
from afair.mcp.server import build_server
from afair.mcp.surface import advertised_surface, canonical_json
from afair.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Iterator

GOLDEN_PATH = Path(__file__).resolve().parent / "goldens" / "mcp_surface.json"


@pytest.fixture(autouse=True)
def _isolated_context() -> Iterator[None]:
    """Each test gets a clean module-level context (build_server sets it)."""
    clear_context()
    try:
        yield
    finally:
        clear_context()


def _settings_for(tmp_path: Path) -> Settings:
    # Must match scripts/dump_mcp_surface.py exactly so the live surface equals
    # the committed golden. The advertised surface is independent of these
    # runtime flags, but keeping them identical removes any doubt.
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=tmp_path,
        cold_path_enabled=False,
        semantic_recall_enabled=False,
    )


async def _live_surface(tmp_path: Path) -> dict[str, Any]:
    server = build_server(_settings_for(tmp_path))
    return await advertised_surface(server)


# ── Layer 1: golden snapshot of the advertised WIRE surface ──────────────────


@pytest.mark.asyncio
async def test_advertised_surface_matches_golden(tmp_path: Path) -> None:
    """The live advertised surface must byte-match the committed golden.

    A red diff here is a CLIENT-CONTRACT change (Invariant I1: the MCP surface
    is versioned and additive; shipped signatures never break). If you INTENDED
    the change:

        uv run python scripts/dump_mcp_surface.py

    then review ``git diff tests/goldens/mcp_surface.json`` — adding a tool /
    param / enum member is fine; removing, renaming, or tightening a shipped
    signature is an I1 violation needing explicit justification. If you did NOT
    intend it, you likely have a v0.1.9-class regression (a schema-shape change
    that silently breaks real-client tool surfacing) — fix the code, not the
    golden.
    """
    assert GOLDEN_PATH.exists(), (
        f"golden missing at {GOLDEN_PATH}; run: uv run python scripts/dump_mcp_surface.py"
    )
    expected = GOLDEN_PATH.read_text(encoding="utf-8")
    actual = canonical_json(await _live_surface(tmp_path))
    assert actual == expected, (
        "Advertised MCP surface changed (I1 client contract). If intended, "
        "regenerate with `uv run python scripts/dump_mcp_surface.py`, review the "
        "`git diff tests/goldens/mcp_surface.json`, and run the compat checklist. "
        "If NOT intended, this is a v0.1.9-class regression that can silently "
        "break real-client tool surfacing — fix the code, not the golden."
    )


# ── Layer 2: schema-shape invariant lints (recursive, over every param) ──────


def _iter_schema_nodes(node: Any) -> Iterator[dict[str, Any]]:
    """Yield every dict node in a JSON-Schema tree (depth-first)."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_schema_nodes(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_schema_nodes(item)


def _is_bare_string_member(member: dict[str, Any]) -> bool:
    """A ``{"type": "string"}`` with no constraining siblings.

    ``const``/``enum``/``format``/``pattern``/``maxLength`` are the escape
    hatches: a string param that carries any of them is a deliberate, narrow
    contract (e.g. a discriminator tag), not the v0.1.9 catch-all widening.
    """
    if member.get("type") != "string":
        return False
    escape_hatches = ("const", "enum", "format", "pattern", "maxLength")
    return not any(k in member for k in escape_hatches)


def _contains_object(node: Any) -> bool:
    """True if ``node`` is object-ish or any of its union members is.

    Recurses through ``anyOf``/``oneOf`` so a wrapped union member (e.g.
    ``anyOf: [{anyOf: [<objects>]}, {"type": "string"}]``) is still detected as
    carrying an object alongside the bare string.
    """
    if not isinstance(node, dict):
        return False
    if node.get("type") == "object" or "properties" in node:
        return True
    return any(_contains_object(sub) for key in ("anyOf", "oneOf") for sub in node.get(key, []))


def _bare_string_pollution_paths(schema: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """Every union in ``schema`` that pairs a bare string with an object member.

    This is the generalized v0.1.9 signature. Returns the offending union
    member-lists (for a readable failure), empty when the schema is clean.
    """
    offenders: list[list[dict[str, Any]]] = []
    for node in _iter_schema_nodes(schema):
        for key in ("anyOf", "oneOf"):
            members = node.get(key)
            if not isinstance(members, list):
                continue
            has_bare_string = any(
                isinstance(m, dict) and _is_bare_string_member(m) for m in members
            )
            if not has_bare_string:
                continue
            has_object = any(
                isinstance(m, dict) and not _is_bare_string_member(m) and _contains_object(m)
                for m in members
            )
            if has_object:
                offenders.append(members)
    return offenders


def _in_out_schemas(surface: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield (tool_name, which, schema) for every non-null in/out schema."""
    for tool in surface["tools"]:
        yield tool["name"], "inputSchema", tool["inputSchema"]
        if tool.get("outputSchema") is not None:
            yield tool["name"], "outputSchema", tool["outputSchema"]


@pytest.mark.asyncio
async def test_input_schemas_are_top_level_objects(tmp_path: Path) -> None:
    """Every tool's top-level inputSchema is ``type: object`` with ``properties``
    and no top-level ``anyOf``/``oneOf``/``$ref`` — the shape claude.ai needs to
    surface the tool at all."""
    surface = await _live_surface(tmp_path)
    for tool in surface["tools"]:
        schema = tool["inputSchema"]
        name = tool["name"]
        assert schema.get("type") == "object", (
            f"{name}.inputSchema top-level type must be 'object', got {schema.get('type')!r}"
        )
        assert "properties" in schema, f"{name}.inputSchema must have top-level 'properties'"
        for forbidden in ("anyOf", "oneOf", "$ref"):
            assert forbidden not in schema, (
                f"{name}.inputSchema must NOT carry a top-level {forbidden!r} "
                f"(a client can't surface a non-object top-level schema — the v0.1.9 class)"
            )


@pytest.mark.asyncio
async def test_no_bare_string_union_pollution(tmp_path: Path) -> None:
    """No param of any tool advertises a bare ``{"type": "string"}`` alongside an
    object member — the generalized v0.1.9 signature. Legit ``str | None`` (no
    object co-member) and constrained strings (const/enum/format/pattern/
    maxLength) pass.

    Recurses over EVERY param of EVERY in/out schema, not a hand-picked few, so
    a future widening on any argument is caught."""
    surface = await _live_surface(tmp_path)
    for name, which, schema in _in_out_schemas(surface):
        offenders = _bare_string_pollution_paths(schema)
        assert not offenders, (
            f"{name}.{which} advertises bare-string-union pollution "
            f"(a {{'type':'string'}} member with no const/enum/format/pattern/maxLength "
            f"sitting in the same union as an object member — the v0.1.9 class that broke "
            f"claude.ai tool surfacing). Offending union(s): {offenders}. "
            f"Make string tolerance a coercion (BeforeValidator/PlainValidator), not a "
            f"schema member, so the object contract stays clean."
        )


@pytest.mark.asyncio
async def test_every_schema_is_valid_draft_2020_12(tmp_path: Path) -> None:
    """Every advertised in/out schema is valid JSON Schema draft 2020-12 — the
    dialect MCP clients validate against."""
    surface = await _live_surface(tmp_path)
    for _name, _which, schema in _in_out_schemas(surface):
        # Raises SchemaError with a precise pointer if the schema is malformed.
        Draft202012Validator.check_schema(schema)


@pytest.mark.asyncio
async def test_pollution_lint_helpers_flag_the_v0_1_9_signature() -> None:
    """Self-check the lint: the exact v0.1.9 shape is flagged, and the legit
    ``str | None`` / constrained-string shapes are not (guards against the lint
    silently going toothless)."""
    # v0.1.9: an object-carrying union member next to a bare string.
    polluted = {
        "properties": {
            "content": {
                "anyOf": [
                    {"anyOf": [{"type": "object", "properties": {"type": {"const": "text"}}}]},
                    {"type": "string"},
                ]
            }
        }
    }
    assert _bare_string_pollution_paths(polluted)

    # Legit str | None — string + null, no object co-member.
    nullable = {"properties": {"q": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}
    assert not _bare_string_pollution_paths(nullable)

    # Constrained string next to an object — deliberate, allowed.
    constrained = {
        "properties": {
            "x": {
                "anyOf": [
                    {"type": "string", "format": "uri"},
                    {"type": "object", "properties": {"k": {"type": "string"}}},
                ]
            }
        }
    }
    assert not _bare_string_pollution_paths(constrained)
