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

    Permissive by design (Invariant I3): unknown content types still get a
    reasonable index from whichever recognized string keys are present. The
    set of recognized keys is additive — new keys may be added forever,
    never removed.

    For inline-text payloads: the text plus context/metadata.
    For object-store payloads: the metadata fields (mime, filename_hint, etc.).
    For observe-event payloads: action/subject/result.
    For compound events: each part's text + label, concatenated.
    The blob bytes themselves are not searchable from here; a future
    Extractor may produce searchable summaries via the Interpretation layer.
    """
    parts: list[str] = []
    text = payload.get("text")
    if isinstance(text, str):
        parts.append(text)
    for key in (
        # content-payload metadata
        "context",
        "filename_hint",
        "mime",
        "type_hint",
        "language",
        # observe-event recognized fields
        "action",
        "subject",
        "result",
        # bi-temporal invalidation payload — humans search for "why did we
        # mark X outdated" → reason is the natural text to index
        "reason",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    # Compound events — walk each part's contribution into the same
    # FTS row. Text parts yield their full text + label; blob parts
    # yield filename + mime + label (bytes aren't text, the extractor
    # will enrich later via the binary-modality dispatch).
    compound_parts = payload.get("parts")
    if isinstance(compound_parts, list):
        for part in compound_parts:
            if not isinstance(part, dict):
                continue
            for key in ("text", "label", "filename_hint", "mime"):
                value = part.get(key)
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


def build_compound_payload(
    *,
    parts: list[dict[str, Any]],
    context: str | None,
    type_hint: str | None = None,
) -> dict[str, Any]:
    """Construct a substrate payload for a compound multi-part event.

    Each part dict is already shaped (text or blob-ref) — the handler
    materializes user-side schema objects into these dicts before
    calling. Compound payloads don't spill the parts list itself; if
    any part is a blob the bytes are already in the object store.
    """
    return {
        "content_type": "compound",
        "parts": parts,
        "context": context,
        "type_hint": type_hint,
    }


def build_blob_ref_payload(
    *,
    blob_hash: str,
    size_bytes: int,
    mime: str,
    filename_hint: str | None,
    context: str | None,
    type_hint: str | None = None,
) -> dict[str, Any]:
    """Construct a binary payload from an ALREADY-uploaded blob.

    Used by ``remember(content=BlobRefContent(...))`` after a client
    streamed bytes via /internal/blob/upload. Bytes are already in the
    object store, we just need to wire the event row to them. Identical
    shape to ``build_binary_payload`` so recall + extractor don't have
    to branch on how the bytes arrived.
    """
    return {
        "content_type": "binary",
        "blob_hash": blob_hash,
        "mime": mime,
        "size_bytes": size_bytes,
        "filename_hint": filename_hint,
        "context": context,
        "type_hint": type_hint,
    }
