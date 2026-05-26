"""MCP server — the stable cross-vendor surface (Invariant I1).

Three tools shipped in v1, locked forever in signature and semantics
(surface frozen 2026-05-26, pre-release):

    remember  — save content to the substrate (text or binary); use the
                ``invalidates`` kwarg to supersede prior facts in one call.
    recall    — single retrieval verb. Modes are kwargs, not separate tools:
                ``query=`` (semantic + FTS), ``by_id=`` / ``by_content_hash=``
                (single fetch), ``stats=True`` (vault overview),
                ``full_payload=True`` (un-truncated bodies).
    observe   — log a structured agent event.

Per I1, signatures here may be EXTENDED additively (new optional fields,
new tools) but never broken. Any change that would alter the call pattern
of an existing tool requires a v2 tool, not an in-place edit.
"""

from __future__ import annotations

from .server import build_server, run

__all__ = ["build_server", "run"]
