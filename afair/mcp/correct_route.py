"""Operator-initiated content correction over the dashboard transport (ADR-0009).

``POST /internal/correct`` is the write path behind the Memory Mirror's
"mark wrong" controls. It carries a discriminated-union body:

  - ``{kind: "event", target_hash, correction_text?, reason?}`` — supersede an
    event (Flavor A: a remembered source is wrong; Flavor B-b1: a whole
    synthesis is wrong — same code path, the synthesis ``content_hash`` is the
    target).
  - ``{kind: "key_point", synthesis_hash, point: {text, index?}, verdict, note?}``
    — suppress or restore ONE served key point of a living synthesis
    (Flavor B-b2) without rejecting the whole synthesis and without re-running
    the LLM.

The route holds ZERO SQL: it authorizes, validates the whole body before any
DB work (typed 4xx per shape error), then calls the substrate writers in
``substrate/content_corrections.py`` on its own ``open_db`` connection (the
``import_route`` precedent — never ``handlers.remember``, which is
ServerContext-coupled).

This is operator-INITIATED, not a proposal to confirm, so it does NOT route
through ``decide_correction`` / ``proposed_corrections`` (ADR-0002 single-write
discipline: no phantom rows). Every write is append-only and reversible
(re-validate / restore), fully ``observe``-logged (I7).

Auth is ``authorize_internal`` (master bearer OR the short-lived, sub-pinned
dashboard JWT) — operator-only, single-tenant (I8).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse

from ..agents.living_syntheses import LIVING_SYNTHESIS_KIND
from ..substrate import open_db, read_event_by_hash
from ..substrate.content_corrections import (
    TargetIsInvalidationError,
    TargetNotFoundError,
    correct_event,
    point_digest,
    review_key_point,
)
from .cors import cors_headers
from .internal_auth import authorize_internal

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

# ── validation bounds (§2) ────────────────────────────────────────────────
MAX_CORRECTION_TEXT_BYTES = 20_000
MAX_REASON_CHARS = 500
MAX_NOTE_CHARS = 500
MAX_POINT_TEXT_CHARS = 4_000
_HASH_PREFIX = "sha256:"
_HEX = frozenset("0123456789abcdef")

# Re-synthesis latency the honest UI copy quotes: the living-synthesis worker
# runs every 6h, so a corrected source is reflected in a fresh synthesis within
# that window (living_syntheses worker interval).
_RESYNTHESIS_WITHIN_SECONDS = 21_600
_RESYNTHESIS_NOTE = (
    "Affected syntheses re-derive from the updated cluster within about 6 hours. "
    "A fresh synthesis may still repeat a claim if the underlying sources are "
    "unchanged — correct the source, or suppress the specific key point."
)


def _unauthorized(request: Request) -> Response:
    return JSONResponse(
        {"error": "unauthorized"},
        status_code=401,
        headers={
            "WWW-Authenticate": 'Bearer realm="correct"',
            **cors_headers(request),
        },
    )


def _error(request: Request, code: str, status: int, message: str | None = None) -> Response:
    body: dict[str, Any] = {"error": code}
    if message is not None:
        body["message"] = message
    return JSONResponse(
        body,
        status_code=status,
        headers={"Cache-Control": "no-store", **cors_headers(request)},
    )


def _normalize_hash(raw: Any) -> str | None:
    """Validate a 64-hex content hash and return its canonical ``sha256:``-prefixed
    form, or None.

    The dashboard may send the hash bare or already prefixed; the substrate
    stores content hashes as ``sha256:<64hex>`` (``payload.content_hash``), so we
    validate the hex and hand the canonical prefixed form back to the substrate
    lookups (which match on the stored value verbatim)."""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if value.startswith(_HASH_PREFIX):
        value = value[len(_HASH_PREFIX) :]
    value = value.lower()
    if len(value) != 64 or any(ch not in _HEX for ch in value):
        return None
    return f"{_HASH_PREFIX}{value}"


async def correct_endpoint(request: Request) -> Response:
    if not authorize_internal(request):
        return _unauthorized(request)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _error(request, "invalid_json", 400)
    if not isinstance(body, dict):
        return _error(request, "body_must_be_object", 400)

    kind = body.get("kind")
    if kind == "event":
        return await _handle_event(request, body)
    if kind == "key_point":
        return await _handle_key_point(request, body)
    return _error(request, "unknown_kind", 400)


async def _handle_event(request: Request, body: dict[str, Any]) -> Response:
    """Flavor A source-wrong / Flavor B-b1 synthesis-wrong — supersede an event."""
    raw_target = body.get("target_hash")
    if raw_target is None or (isinstance(raw_target, str) and not raw_target.strip()):
        return _error(request, "target_hash_required", 400)
    normalized = _normalize_hash(raw_target)
    if normalized is None:
        return _error(request, "target_hash_malformed", 400)

    correction_text = body.get("correction_text")
    if correction_text is not None:
        if not isinstance(correction_text, str):
            return _error(request, "correction_text_must_be_string", 400)
        correction_text = correction_text.strip()
        if not correction_text:
            correction_text = None
        elif len(correction_text.encode("utf-8")) > MAX_CORRECTION_TEXT_BYTES:
            return _error(request, "correction_text_too_large", 413)

    reason = body.get("reason")
    if reason is not None:
        if not isinstance(reason, str):
            return _error(request, "reason_must_be_string", 400)
        reason = reason.strip()
        if not reason:
            reason = None
        elif len(reason) > MAX_REASON_CHARS:
            return _error(request, "reason_too_long", 400)

    conn = open_db(Path(request.app.state.settings.vault_dir))
    try:
        try:
            result = correct_event(
                conn,
                target_hash=normalized,
                correction_text=correction_text,
                reason=reason,
            )
        except TargetNotFoundError:
            return _error(request, "target_not_found", 404)
        except TargetIsInvalidationError:
            return _error(request, "target_is_invalidation", 400)
    finally:
        conn.close()

    # 200 when nothing new was written (fully-deduplicated no-op / already
    # invalidated with no correction text), 201 when a fresh record landed.
    wrote_something = not result.already_invalidated or (
        result.correction_event_id is not None and not result.deduplicated
    )
    status_code = 201 if wrote_something else 200
    return JSONResponse(
        {
            "ok": True,
            "kind": "event",
            "target_hash": result.target_hash,
            "already_invalidated": result.already_invalidated,
            "invalidation_event_id": result.invalidation_event_id,
            "correction_event_id": result.correction_event_id,
            "correction_content_hash": result.correction_content_hash,
            "deduplicated": result.deduplicated,
            "resynthesis": {
                "expected_within_seconds": _RESYNTHESIS_WITHIN_SECONDS,
                "note": _RESYNTHESIS_NOTE,
            },
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store", **cors_headers(request)},
    )


async def _handle_key_point(request: Request, body: dict[str, Any]) -> Response:
    """Flavor B-b2 — suppress or restore one served key point of a synthesis."""
    raw_synthesis = body.get("synthesis_hash")
    if raw_synthesis is None or (isinstance(raw_synthesis, str) and not raw_synthesis.strip()):
        return _error(request, "synthesis_hash_required", 400)
    normalized = _normalize_hash(raw_synthesis)
    if normalized is None:
        return _error(request, "synthesis_hash_malformed", 400)

    verdict = body.get("verdict")
    if verdict not in ("suppress", "restore"):
        return _error(request, "invalid_verdict", 400)

    point = body.get("point")
    if not isinstance(point, dict):
        return _error(request, "point_required", 400)
    point_text = point.get("text")
    if not isinstance(point_text, str) or not point_text.strip():
        return _error(request, "point_text_required", 400)
    point_text = point_text.strip()
    if len(point_text) > MAX_POINT_TEXT_CHARS:
        return _error(request, "point_text_too_long", 400)

    note = body.get("note")
    if note is not None:
        if not isinstance(note, str):
            return _error(request, "note_must_be_string", 400)
        note = note.strip()
        if not note:
            note = None
        elif len(note) > MAX_NOTE_CHARS:
            return _error(request, "note_too_long", 400)

    conn = open_db(Path(request.app.state.settings.vault_dir))
    try:
        synthesis = read_event_by_hash(conn, normalized)
        if synthesis is None:
            return _error(request, "synthesis_not_found", 404)
        if synthesis.kind != LIVING_SYNTHESIS_KIND:
            return _error(request, "not_a_synthesis", 400)

        # The point must match a served key point of THIS synthesis (matched by
        # digest of normalized text — key points carry no stable id). A reword
        # misses (documented b3 limitation); an unknown point is a typed 404.
        served = synthesis.payload.get("key_points")
        served_digests = (
            {
                point_digest(item["point"])
                for item in served
                if isinstance(item, dict) and isinstance(item.get("point"), str)
            }
            if isinstance(served, list)
            else set()
        )
        if point_digest(point_text) not in served_digests:
            return _error(request, "key_point_not_found", 404)

        cluster_id = synthesis.payload.get("cluster_id")
        if cluster_id is not None and not isinstance(cluster_id, str):
            cluster_id = None

        result = review_key_point(
            conn,
            synthesis_hash=normalized,
            point_text=point_text,
            verdict=verdict,
            cluster_id=cluster_id,
            note=note,
        )
    finally:
        conn.close()

    # 200 for an idempotent no-op (already in the requested state), 201 when a
    # fresh review row was appended.
    status_code = 200 if result.status.startswith("already_") else 201
    return JSONResponse(
        {
            "ok": True,
            "kind": "key_point",
            "synthesis_hash": result.synthesis_hash,
            "point_digest": result.point_digest,
            "verdict": result.verdict,
            "status": result.status,
            "interpretation_id": result.interpretation_id,
            "version": result.version,
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store", **cors_headers(request)},
    )
