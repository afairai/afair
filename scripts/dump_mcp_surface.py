#!/usr/bin/env python3
"""Regenerate the advertised-MCP-surface golden.

Run this when a client-facing MCP surface change is *intended*:

    uv run python scripts/dump_mcp_surface.py

It builds a throwaway in-memory server on a temp vault (cold path +
semantic recall disabled so no background worker or embedding provider is
touched — the surface is independent of runtime state), captures the
advertised WIRE surface via ``afair.mcp.surface.advertised_surface``, and
writes ``tests/goldens/mcp_surface.json``.

Then: review the ``git diff`` of the golden. A diff is an Invariant-I1 event
(the MCP contract is versioned and additive). Adding a tool / param / enum
member is fine; removing, renaming, or tightening a shipped signature is an I1
violation that needs explicit justification. See
``docs/clients/compat-checklist.md`` (Layer 4, when it lands) before blessing.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from afair.mcp.server import build_server
from afair.mcp.surface import advertised_surface, canonical_json
from afair.settings import Settings

GOLDEN_PATH = Path(__file__).resolve().parent.parent / "tests" / "goldens" / "mcp_surface.json"


def _settings(vault_dir: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=vault_dir,
        cold_path_enabled=False,
        semantic_recall_enabled=False,
    )


async def _build_surface(vault_dir: Path) -> str:
    server = build_server(_settings(vault_dir))
    surface = await advertised_surface(server)
    return canonical_json(surface)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        golden = asyncio.run(_build_surface(Path(tmp)))
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(golden, encoding="utf-8")
    print(f"wrote {GOLDEN_PATH} ({len(golden)} bytes)")


if __name__ == "__main__":
    main()
