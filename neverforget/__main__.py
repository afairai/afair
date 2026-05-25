"""Entry point. `python -m neverforget` or the `neverforget` console script.

Phase 0: prints settings and exits. Real server arrives with task #3 (MCP server).
"""

from __future__ import annotations

import sys

from .settings import load_settings


def main() -> int:
    settings = load_settings()
    # Intentionally minimal output. Real server boot lands in task #3.
    print(f"environment           : {settings.environment}")
    print(f"log_level             : {settings.log_level}")
    print(f"vault_dir             : {settings.vault_dir}")
    print(f"mcp_host:port         : {settings.mcp_host}:{settings.mcp_port}")
    print(f"extractor_model       : {settings.extractor_model}")
    print(f"embedding_model       : {settings.embedding_model}")
    print(f"inline_text_max_bytes : {settings.inline_text_max_bytes}")
    print()
    print("[scaffold OK — MCP server not yet implemented; see task #3]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
