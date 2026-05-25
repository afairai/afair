"""The append-only substrate — Invariants I2 and I3.

Two-tier storage:
    vault/substrate.db       SQLite (events, FTS5, interpretations)
    vault/objects/<aa>/<...> filesystem object store (binary + large text)

Public API surface — stable for the lifetime of the project. New helpers
may be added; existing signatures should not change without an explicit
constitutional review.
"""

from __future__ import annotations

from .db import init_db, open_db
from .events import (
    Event,
    iter_events,
    read_event_by_hash,
    read_event_by_id,
    write_event,
)
from .objects import object_path, read_object, write_object
from .payload import (
    build_binary_payload,
    build_text_payload,
    canonical_json,
    content_hash,
    derive_searchable_text,
)
from .schema import SCHEMA_VERSION
from .search import hybrid_search, search_fts, search_vec

__all__ = [
    "SCHEMA_VERSION",
    "Event",
    "build_binary_payload",
    "build_text_payload",
    "canonical_json",
    "content_hash",
    "derive_searchable_text",
    "hybrid_search",
    "init_db",
    "iter_events",
    "object_path",
    "open_db",
    "read_event_by_hash",
    "read_event_by_id",
    "read_object",
    "search_fts",
    "search_vec",
    "write_event",
    "write_object",
]
