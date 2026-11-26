"""Shared model defaults, without importing settings, providers or agents.

Environment and per-call overrides remain authoritative. Keep this module
dependency-free so low-level extractors can share the server's default.
"""

DEFAULT_VISION_MODEL = "anthropic/claude-haiku-5-5"
