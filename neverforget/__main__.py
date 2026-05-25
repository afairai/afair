"""Entry point — `python -m neverforget` or the `neverforget` console script.

Boots the MCP server over Streamable HTTP. The same binary runs locally,
in self-hosted contexts, and on a managed Fly machine — only env vars differ.
"""

from __future__ import annotations

import sys

from .mcp import run as run_server
from .settings import load_settings


def main() -> int:
    settings = load_settings()
    print(
        f"[neverforget] {settings.environment} | "
        f"vault={settings.vault_dir} | "
        f"listen={settings.mcp_host}:{settings.mcp_port} | "
        f"extractor={settings.extractor_model}"
    )
    run_server(settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
