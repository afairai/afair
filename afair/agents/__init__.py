"""Agents that operate over the substrate.

The Extractor covers the warm path; the cold-path workers (salience,
surprise, mode switching, temporal decay, entity canonicalization and
dedup, conflict resolution, consolidation, pruning, self-improvement)
live in their own modules here. A dedicated Schema-Evolver agent is
planned but not yet implemented; the entity dedup and audit workers
cover its v0 duties. The interfaces are designed so each future agent
can be added without disturbing the existing ones.
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
