"""MCP server — the stable cross-vendor surface (Invariant I1).

Four tools shipped in v1, locked forever in signature and semantics:
    remember     — save content to the substrate (text or binary)
    recall       — retrieve relevant memories
    list_context — survey what's in the vault
    observe      — log a structured agent event

Per I1, signatures here may be EXTENDED additively (new optional fields,
new tools) but never broken. Any change that would alter the call pattern
of an existing tool requires a v2 tool, not an in-place edit.
"""

from __future__ import annotations

from .server import build_server, run

__all__ = ["build_server", "run"]
