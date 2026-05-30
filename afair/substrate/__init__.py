"""The append-only substrate — Invariants I2 and I3.

Two-tier storage:
    vault/substrate.db       SQLite (events, FTS5, interpretations)
    vault/objects/<aa>/<...> filesystem object store (binary + large text)

Public API surface — stable for the lifetime of the project. New helpers
may be added; existing signatures should not change without an explicit
constitutional review.
"""

from __future__ import annotations

from .checkpoint import start_checkpoint_loop
from .db import init_db, open_db
from .entities import (
    EdgeInvalidation,
    Entity,
    EntityEdge,
    EntityMention,
    EntityMerge,
    entity_id,
    find_edges_for_source_event,
    find_entity_by_name,
    iter_edges_for_entity,
    iter_mentions_for_event,
    read_edge_invalidations,
    read_edges_by_source_event_ids,
    read_entities_batch,
    read_entity_by_id,
    read_mentions_batch,
    resolve_canonical,
    resolve_canonical_batch,
    write_edge_invalidation,
    write_entity,
    write_entity_edge,
    write_entity_mention,
    write_entity_merge,
)
from .event_records import iter_records, read_record, record_exists, record_path, write_record
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
from .recovery import backfill_records_from_events, rebuild_events_from_records
from .schema import SCHEMA_VERSION
from .search import hybrid_search, rrf_merge, search_fts, search_vec

__all__ = [
    "SCHEMA_VERSION",
    "EdgeInvalidation",
    "Entity",
    "EntityEdge",
    "EntityMention",
    "EntityMerge",
    "Event",
    "backfill_records_from_events",
    "build_binary_payload",
    "build_text_payload",
    "canonical_json",
    "content_hash",
    "derive_searchable_text",
    "entity_id",
    "find_edges_for_source_event",
    "find_entity_by_name",
    "hybrid_search",
    "init_db",
    "iter_edges_for_entity",
    "iter_events",
    "iter_mentions_for_event",
    "iter_records",
    "object_path",
    "open_db",
    "read_edge_invalidations",
    "read_edges_by_source_event_ids",
    "read_entities_batch",
    "read_entity_by_id",
    "read_event_by_hash",
    "read_event_by_id",
    "read_mentions_batch",
    "read_object",
    "read_record",
    "rebuild_events_from_records",
    "record_exists",
    "record_path",
    "resolve_canonical",
    "resolve_canonical_batch",
    "rrf_merge",
    "search_fts",
    "search_vec",
    "start_checkpoint_loop",
    "write_edge_invalidation",
    "write_entity",
    "write_entity_edge",
    "write_entity_mention",
    "write_entity_merge",
    "write_event",
    "write_object",
    "write_record",
]
