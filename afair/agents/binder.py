"""Bind agent v0 — auto-link semantically-similar events.

After the Extractor stores an event's embedding, the Bind agent queries
events_vec for nearby vectors and writes a ``binder:v0`` row into the
interpretations table listing the top-K most similar PRIOR events.

This is the I3-compatible answer to the manual "should I link these
two related events?" question: the Bind agent does it automatically,
storing edges in the Interpretation layer without touching the
immutable substrate.

Phase 1 v0 is minimum-viable:
  - top_k = 3 nearest neighbors (excluding the source event itself)
  - no similarity threshold — store the data; future passes can prune
  - no human-curated edges (manual parent_hashes still work too)
  - fails soft — bind failure does not roll back extraction
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING, Any

import structlog

from .interpretation import write_interpretation

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

    from ..substrate.events import Event

log = structlog.get_logger(__name__)

BINDER_VERSION = 1
BINDER_PRODUCED_BY = "binder:v0"
DEFAULT_TOP_K = 3


def find_and_record_links(
    conn: sqlite3.Connection,
    *,
    event: Event,
    embedding: Sequence[float],
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, Any] | None:
    """Find the top-K most similar prior events and record them as a bind row.

    Returns the extraction payload that was written (or None if no neighbors
    were found / something went wrong). Failure is soft: an exception is
    logged but does not propagate.
    """
    try:
        # Pack the source vector for sqlite-vec.
        packed = struct.pack(f"<{len(embedding)}f", *embedding)
        # k = top_k + 1 because the source event itself will be among the
        # nearest neighbors of its own embedding. We filter it out below.
        rows = conn.execute(
            """
            SELECT content_hash, distance FROM events_vec
            WHERE embedding MATCH ? AND k = ?
            ORDER BY distance
            """,
            (packed, top_k + 1),
        ).fetchall()
    except Exception as e:
        log.warning("binder.search_failed", event_id=event.id, error=str(e))
        return None

    links: list[dict[str, Any]] = []
    for row in rows:
        neighbor_hash = row["content_hash"]
        if neighbor_hash == event.content_hash:
            continue  # skip self
        links.append({"event_hash": neighbor_hash, "distance": float(row["distance"])})
        if len(links) >= top_k:
            break

    if not links:
        return None

    extraction: dict[str, Any] = {
        "status": "success",
        "type": "bind",
        "links": links,
    }
    try:
        write_interpretation(
            conn,
            event=event,
            version=BINDER_VERSION,
            produced_by=BINDER_PRODUCED_BY,
            extraction=extraction,
        )
    except Exception as e:
        log.warning("binder.write_failed", event_id=event.id, error=str(e))
        return None

    log.info("binder.linked", event_id=event.id, link_count=len(links), top_k=top_k)
    return extraction


def get_linked_event_ids(conn: sqlite3.Connection, event_hash: str) -> list[str]:
    """Return the content_hashes the Bind agent linked to this event.

    Used by recall to surface "related events" alongside each hit.
    Empty list when no bind record exists (e.g., extraction is still
    in flight or there were no neighbors).
    """
    import json

    row = conn.execute(
        """
        SELECT extraction FROM interpretations
        WHERE event_hash = ? AND produced_by = ?
        ORDER BY produced_at DESC
        """,
        (event_hash, BINDER_PRODUCED_BY),
    ).fetchone()
    if row is None:
        return []
    data = json.loads(row["extraction"])
    links = data.get("links", [])
    return [link["event_hash"] for link in links if "event_hash" in link]
