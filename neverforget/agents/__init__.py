"""Agents that operate over the substrate.

Phase 0 ships the Extractor (warm path only). The full swarm — Retriever,
Consolidator, Schema-Evolver, Pruner, Conflict-Resolver, Bind, Salience —
arrives in later phases. The interfaces here are designed so each future
agent can be added without disturbing this one.
"""

from __future__ import annotations

from .extractor import extract_sync, schedule_extraction
from .interpretation import (
    Interpretation,
    read_latest_interpretation,
    write_failed_interpretation,
    write_interpretation,
)

__all__ = [
    "Interpretation",
    "extract_sync",
    "read_latest_interpretation",
    "schedule_extraction",
    "write_failed_interpretation",
    "write_interpretation",
]
