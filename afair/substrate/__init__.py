"""The append-only substrate — Invariants I2 and I3.

Two-tier storage:
    vault/substrate.db       SQLite (events, FTS5, interpretations)
    vault/objects/<aa>/<...> filesystem object store (binary + large text)

Public API surface — stable for the lifetime of the project. New helpers
may be added; existing signatures should not change without an explicit
constitutional review.
"""

from __future__ import annotations

from . import pipeline_events
from .checkpoint import start_checkpoint_loop
from .db import init_db, open_db
from .entities import (
    EdgeInvalidation,
    EdgeReview,
    Entity,
    EntityEdge,
    EntityMention,
    EntityMerge,
    entity_id,
    find_edges_for_source_event,
    find_entity_by_name,
    iter_edges_for_entity,
    iter_mentions_for_event,
    latest_edge_review,
    read_edge_invalidations,
    read_edges_by_source_event_ids,
    read_entities_batch,
    read_entity_by_id,
    read_mentions_batch,
    record_edge_review,
    resolve_canonical,
    resolve_canonical_batch,
    write_edge_invalidation,
    write_entity,
    write_entity_edge,
    write_entity_mention,
    write_entity_merge,
)
from .events import (
    Event,
    iter_events,
    read_event_by_hash,
    read_event_by_id,
    write_event,
    write_event_with_status,
)
from .objects import (
    StreamingObjectWriter,
    object_exists,
    object_path,
    object_plaintext_size,
    object_size,
    read_object,
    write_object,
)
from .payload import (
    build_binary_payload,
    build_blob_ref_payload,
    build_compound_payload,
    build_text_payload,
    canonical_json,
    content_hash,
    derive_searchable_text,
)
from .schema import SCHEMA_VERSION
from .search import hybrid_search, rrf_merge, search_fts, search_vec

__all__ = [
    "SCHEMA_VERSION",
    "EdgeInvalidation",
    "EdgeReview",
    "Entity",
    "EntityEdge",
    "EntityMention",
    "EntityMerge",
    "Event",
    "StreamingObjectWriter",
    "build_binary_payload",
    "build_blob_ref_payload",
    "build_compound_payload",
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
    "latest_edge_review",
    "object_exists",
    "object_path",
    "object_plaintext_size",
    "object_size",
    "open_db",
    "pipeline_events",
    "read_edge_invalidations",
    "read_edges_by_source_event_ids",
    "read_entities_batch",
    "read_entity_by_id",
    "read_event_by_hash",
    "read_event_by_id",
    "read_mentions_batch",
    "read_object",
    "record_edge_review",
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
    "write_event_with_status",
    "write_object",
]
