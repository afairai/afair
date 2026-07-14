"""User-initiated import into the append-only vault.

The browser normalizes vendor exports into text items and sends them directly
to the user's own single-tenant machine. The shared control plane never sees
the imported content. Each item becomes an ordinary immutable remember event,
so existing extraction, clustering, correction, and export paths apply.
"""

from __future__ import annotations

import json
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse

from ..agents import schedule_extraction
from ..substrate import open_db, write_event_with_status
from ..substrate import pipeline_events as pe
from .cors import cors_headers
from .internal_auth import authorize_internal

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

ALLOWED_SOURCES = frozenset({"chatgpt", "claude", "obsidian", "notion", "files"})
MAX_ITEMS = 500
MAX_ITEM_BYTES = 100_000
MAX_TOTAL_BYTES = 8_000_000
MAX_TITLE_CHARS = 300
MAX_PATH_CHARS = 1000


def _unauthorized(request: Request) -> Response:
    return JSONResponse(
        {"error": "unauthorized"},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer realm="import"', **cors_headers(request)},
    )


async def import_endpoint(request: Request) -> Response:
    if not authorize_internal(request):
        return _unauthorized(request)
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _error(request, "invalid_json", 400)
    if not isinstance(body, dict):
        return _error(request, "body_must_be_object", 400)

    source = body.get("source")
    items = body.get("items")
    if source not in ALLOWED_SOURCES:
        return _error(request, "unsupported_source", 400)
    if not isinstance(items, list) or not items:
        return _error(request, "items_required", 400)
    if len(items) > MAX_ITEMS:
        return _error(request, "too_many_items", 413)

    normalized: list[dict[str, Any]] = []
    total_bytes = 0
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            return _error(request, f"item_{index}_must_be_object", 400)
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            return _error(request, f"item_{index}_text_required", 400)
        text = text.strip()
        size = len(text.encode("utf-8"))
        if size > MAX_ITEM_BYTES:
            return _error(request, f"item_{index}_too_large", 413)
        total_bytes += size
        if total_bytes > MAX_TOTAL_BYTES:
            return _error(request, "batch_too_large", 413)
        normalized.append(
            {
                "text": text,
                "title": _bounded_string(item.get("title"), MAX_TITLE_CHARS),
                "path": _bounded_string(item.get("path"), MAX_PATH_CHARS),
                "external_id": _bounded_string(item.get("external_id"), 500),
                "created_at": _valid_created_at(item.get("created_at")),
            }
        )

    conn = open_db(Path(request.app.state.settings.vault_dir))
    inserted = 0
    deduplicated = 0
    event_ids: list[str] = []
    try:
        for item in normalized:
            context_parts = [f"Imported from {source}"]
            if item["title"]:
                context_parts.append(item["title"])
            if item["path"]:
                context_parts.append(item["path"])
            payload = {
                "content_type": "text",
                "text": item["text"],
                "context": ": ".join(context_parts),
                "type_hint": "imported_memory",
                "import_source": source,
                "import_title": item["title"],
                "import_path": item["path"],
                "import_external_id": item["external_id"],
            }
            event, was_inserted = write_event_with_status(
                conn,
                origin="user",
                kind="remember",
                payload=payload,
                created_at=item["created_at"],
            )
            event_ids.append(event.id)
            if was_inserted:
                inserted += 1
                pe.record(
                    conn,
                    event_id=event.id,
                    event_hash=event.content_hash,
                    stage=pe.STAGE_EVENT_WRITTEN,
                    producer=f"import:{source}",
                )
                # Isolated route tests do not install a ServerContext. A
                # production server always does. The durable event is still
                # picked up by the normal cold-path recovery flow.
                with suppress(RuntimeError):
                    schedule_extraction(event.id)
            else:
                deduplicated += 1
    finally:
        conn.close()

    return JSONResponse(
        {
            "ok": True,
            "source": source,
            "received": len(normalized),
            "inserted": inserted,
            "deduplicated": deduplicated,
            "event_ids": event_ids,
        },
        status_code=201,
        headers={"Cache-Control": "no-store", **cors_headers(request)},
    )


def _error(request: Request, code: str, status: int) -> Response:
    return JSONResponse({"error": code}, status_code=status, headers=cors_headers(request))


def _bounded_string(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped[:limit] if stripped else None


def _valid_created_at(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    normalized = parsed.astimezone(UTC)
    # Clamp a future-dated import to now: a user-supplied created_at in the
    # future would otherwise pin the newest-events discovery window and daily
    # consolidation on a timestamp that never arrives. Past dates are kept
    # verbatim (genuine historical imports).
    now = datetime.now(UTC)
    if normalized > now:
        normalized = now
    return normalized.isoformat()
