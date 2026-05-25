"""Canonical JSON, content hashing, and payload-shape builders.

These helpers enforce I2 (content-addressed) and I3 (forever readable):
every payload is built into one of a small set of self-describing shapes,
discriminated by the ``content_type`` key. New shapes are added by appending
new ``content_type`` values, never by mutating existing ones.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from .objects import write_object

if TYPE_CHECKING:
    from pathlib import Path


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8 preserved."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(
    *,
    kind: str,
    origin: str,
    payload: dict[str, Any],
    parent_hashes: list[str] | None,
) -> str:
    """sha256 over the canonical event identity.

    Identical (kind, origin, payload, parent_hashes) → identical hash, so the
    UNIQUE constraint on ``content_hash`` naturally dedupes a re-issued call.

    Timestamp is intentionally excluded — re-issuing the same logical event
    later is still the same observation. ``parent_hashes`` is sorted before
    hashing so order does not affect identity; if order matters semantically,
    encode it inside ``payload``.
    """
    identity = {
        "kind": kind,
        "origin": origin,
        "payload": payload,
        "parent_hashes": sorted(parent_hashes) if parent_hashes else None,
    }
    serialized = canonical_json(identity).encode("utf-8")
    return f"sha256:{hashlib.sha256(serialized).hexdigest()}"


def derive_searchable_text(payload: dict[str, Any]) -> str:
    """Compose the text body that FTS5 indexes for an event.

    For inline-text payloads: the text plus any string metadata.
    For object-store payloads: the metadata fields (context, filename_hint,
    mime, type_hint). The blob itself is not searchable from here; a future
    Extractor may produce searchable summaries that land in the Interpretation
    layer instead.
    """
    parts: list[str] = []
    text = payload.get("text")
    if isinstance(text, str):
        parts.append(text)
    for key in ("context", "filename_hint", "mime", "type_hint", "language"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    return "\n".join(parts)


def build_text_payload(
    *,
    text: str,
    context: str | None,
    type_hint: str | None,
    vault_dir: Path,
    inline_text_max_bytes: int,
    language: str | None = None,
) -> dict[str, Any]:
    """Construct a substrate payload for text content.

    Spills to the filesystem object store when the UTF-8 encoded text exceeds
    ``inline_text_max_bytes``; otherwise stays inline in the SQLite row.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= inline_text_max_bytes:
        return {
            "content_type": "text",
            "text": text,
            "context": context,
            "type_hint": type_hint,
            "language": language,
        }
    blob_hash = write_object(vault_dir, encoded)
    return {
        "content_type": "text-large",
        "blob_hash": blob_hash,
        "mime": "text/plain; charset=utf-8",
        "size_bytes": len(encoded),
        "context": context,
        "type_hint": type_hint,
        "language": language,
    }


def build_binary_payload(
    *,
    data: bytes,
    mime: str,
    filename_hint: str | None,
    context: str | None,
    vault_dir: Path,
    type_hint: str | None = None,
) -> dict[str, Any]:
    """Construct a substrate payload for binary content (always spills)."""
    blob_hash = write_object(vault_dir, data)
    return {
        "content_type": "binary",
        "blob_hash": blob_hash,
        "mime": mime,
        "size_bytes": len(data),
        "filename_hint": filename_hint,
        "context": context,
        "type_hint": type_hint,
    }
