"""Entry point — `python -m afair` or the `afair` console script.

Boots the MCP server over Streamable HTTP. The same binary runs locally,
in self-hosted contexts, and on a managed Fly machine — only env vars differ.
"""

from __future__ import annotations

import sys

from .mcp import run as run_server
from .observability import configure_logging, init_sentry
from .settings import load_settings


def main() -> int:
    # Init Sentry before anything else so even bootstrap errors land.
    # No-op when SENTRY_DSN is unset (local dev, self-hosted opt-out).
    init_sentry()
    settings = load_settings()
    # Install the redacting structlog chain before any worker logs. Every
    # log line from here on is masked for credentials/PII and length-capped.
    configure_logging(environment=settings.environment)
    print(
        f"[afair] {settings.environment} | "
        f"vault={settings.vault_dir} | "
        f"listen={settings.mcp_host}:{settings.mcp_port} | "
        f"extractor={settings.extractor_model}"
    )
    run_server(settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
