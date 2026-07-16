"""Operator decision surface for the correction/review queue (Phase 1).

Two browser-facing /internal routes that make the Memory Mirror actionable:

  - ``GET  /internal/corrections`` — the same open-proposal queue the recall
    verb surfaces (``_pending_correction_views``), byte-identical to what the
    AI client sees, plus a structured ``detail`` per item for the dashboard's
    controls;
  - ``POST /internal/decide`` — one operator decision, routed through the
    SINGLE mutation point ``decide_correction`` (ADR-0002). The route contains
    ZERO SQL against ``proposed_corrections`` (or any proposal table): it is a
    transport over the same semantics the MCP ``recall(decide=)`` loop uses, so
    the two paths can never diverge.

Auth is the browser-facing ``authorize_internal`` (master bearer OR the
short-lived, sub-pinned dashboard JWT) — operator-only, single-tenant (I8).
Ownership is structural: the JWT sub must equal this machine's one identity,
so there is no per-request ownership check to get wrong.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

from ..substrate import open_db
from ..substrate.corrections import decide_correction
from . import handlers
from .cors import cors_headers
from .internal_auth import authorize_internal
from .schemas import MAX_PENDING_LIMIT

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

DECIDED_BY_DASHBOARD = "operator:dashboard"
"""Provenance stamp for a decision that came through the /account dashboard.
Distinct from the MCP-client ``operator`` so an audit can tell the two apart.
"""

MAX_PROPOSAL_ID_CHARS = 100
MAX_TO_KIND_CHARS = 100

# The exact verdict vocabulary of CorrectionDecision.verdict (schemas.py) —
# validated here so a bad value is a typed 400 before any DB work. The registry
# validation of to_kind stays INSIDE decide_correction (the single mutation
# point); this route never duplicates it.
_VALID_VERDICTS = frozenset({"confirm", "reject", "retract", "revert"})


def _unauthorized(request: Request) -> Response:
    return JSONResponse(
        {"error": "unauthorized"},
        status_code=401,
        headers={
            "WWW-Authenticate": 'Bearer realm="corrections"',
            **cors_headers(request),
        },
    )


def _error(request: Request, code: str, status: int) -> Response:
    return JSONResponse(
        {"error": code},
        status_code=status,
        headers={"Cache-Control": "no-store", **cors_headers(request)},
    )


async def corrections_list_endpoint(request: Request) -> Response:
    """GET /internal/corrections?limit=50&offset=0 — the open review queue.

    Reuses ``handlers._pending_correction_views`` so every item is byte-identical
    to the recall pending view (same order, same fields), then attaches a
    structured ``detail`` payload per item for the dashboard's decision controls.
    """
    if not authorize_internal(request):
        return _unauthorized(request)

    limit = _clamp_int(request.query_params.get("limit"), default=50, lo=1, hi=MAX_PENDING_LIMIT)
    offset = _clamp_int(request.query_params.get("offset"), default=0, lo=0, hi=10_000_000)

    conn = open_db(Path(request.app.state.settings.vault_dir))
    try:
        views = handlers._pending_correction_views(conn, limit=limit, offset=offset)
        details = handlers._pending_correction_details(conn, limit=limit, offset=offset)
    finally:
        conn.close()

    pending = [
        {
            "id": v.id,
            "kind": v.kind,
            "entity_id": v.entity_id,
            "entity_name": v.entity_name,
            "prompt": v.prompt,
            "evidence": v.evidence,
            "confidence": v.confidence,
            "subject_slug": v.subject_slug,
            "detail": details.get(v.id),
        }
        for v in views
    ]

    return JSONResponse(
        {
            "generated_at": datetime.now(UTC).isoformat(),
            "count": len(pending),
            "pending": pending,
        },
        headers={"Cache-Control": "no-store", **cors_headers(request)},
    )


async def decide_endpoint(request: Request) -> Response:
    """POST /internal/decide {proposal_id, verdict, to_kind?} — one decision.

    Field names match ``CorrectionDecision``. All shape validation happens here,
    before the DB; the kind-registry validation of ``to_kind`` stays inside
    ``decide_correction`` (not duplicated). The handler calls
    ``decide_correction`` and NOTHING else touches the proposal tables — the
    single-mutation-point invariant (ADR-0002) is preserved by construction.
    """
    if not authorize_internal(request):
        return _unauthorized(request)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _error(request, "invalid_json", 400)
    if not isinstance(body, dict):
        return _error(request, "body_must_be_object", 400)

    proposal_id = body.get("proposal_id")
    if not isinstance(proposal_id, str) or not proposal_id.strip():
        return _error(request, "proposal_id_required", 400)
    proposal_id = proposal_id.strip()
    if len(proposal_id) > MAX_PROPOSAL_ID_CHARS:
        return _error(request, "proposal_id_too_long", 400)

    verdict = body.get("verdict")
    if verdict not in _VALID_VERDICTS:
        return _error(request, "invalid_verdict", 400)

    to_kind = body.get("to_kind")
    if to_kind is not None:
        if not isinstance(to_kind, str):
            return _error(request, "invalid_to_kind", 400)
        to_kind = to_kind.strip() or None
        if to_kind is not None and len(to_kind) > MAX_TO_KIND_CHARS:
            return _error(request, "to_kind_too_long", 400)

    conn = open_db(Path(request.app.state.settings.vault_dir))
    try:
        try:
            outcome = decide_correction(
                conn,
                proposal_id=proposal_id,
                verdict=verdict,
                to_kind=to_kind,
                decided_by=DECIDED_BY_DASHBOARD,
            )
        except ValueError as exc:
            # A verdict/to_kind that shape-validates but is semantically invalid
            # for this proposal (e.g. an unknown to_kind slug, or 'revert' on a
            # non-ontology id). decide_correction is the sole authority on that.
            return JSONResponse(
                {"error": "invalid_decision", "message": str(exc)},
                status_code=400,
                headers={"Cache-Control": "no-store", **cors_headers(request)},
            )
    finally:
        conn.close()

    status_code = _STATUS_TO_HTTP.get(outcome.status, 200)
    return JSONResponse(
        {"proposal_id": outcome.proposal_id, "status": outcome.status, "note": outcome.note},
        status_code=status_code,
        headers={"Cache-Control": "no-store", **cors_headers(request)},
    )


# Response mapping (§2.2): applied/confirmed/rejected/reverted → 200;
# already_decided → 200 (idempotent success, a no-op is not an error);
# not_found → 404; everything else defaults to 200 with the status in the body.
_STATUS_TO_HTTP: dict[str, int] = {
    "applied": 200,
    "confirmed": 200,
    "rejected": 200,
    "reverted": 200,
    "not_applied": 200,
    "already_decided": 200,
    "not_found": 404,
}


def _clamp_int(raw: str | None, *, default: int, lo: int, hi: int) -> int:
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, value))
