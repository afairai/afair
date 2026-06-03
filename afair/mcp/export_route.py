"""Vault export endpoint.

Streams the whole vault out as JSON Lines. One line per record,
records ordered by ``created_at`` ascending so a downstream reader
can replay the timeline in order without sorting.

Record shape:
  {"kind": "event",         ...event row + parsed payload}
  {"kind": "interpretation", ...interpretation row with extraction JSON}
  {"kind": "entity",        ...canonical entity}
  {"kind": "entity_mention", ...mention linking an event to an entity}
  {"kind": "entity_edge",   ...directed edge between two entities}

Blob content is NOT inlined by default — the JSONL references blobs
by hash. A separate ``?blobs=inline`` mode base64-encodes blob bytes
for callers who need an air-gapped copy.

Auth: the endpoint accepts EITHER the regular ``AFAIR_AUTH_TOKEN``
(the same bearer used by every MCP request — what the user has from
their onboarding email) OR the scoped ``AFAIR_EXPORT_TOKEN`` if set.

Why both: the user needs to be able to export their own vault using
the credential they already have. The scoped token exists for
automation (a backup cron with no MCP write capability) and is
optional. Single-tenant by design means there is no other tenant to
protect from — anyone who can auth to this machine already sees the
whole vault.

Why HTTP and not an MCP-tool argument: streaming N MB of JSONL
through the MCP JSON-RPC envelope is awkward and forces the entire
response into memory. A regular HTTP route with chunked transfer
encoding scales to any vault size without buffering.

Stable HTTP surface per the I3 commitment: this endpoint and its
record shape stay back-compatible. New record kinds are additive
(callers ignore unknown kinds).
"""

from __future__ import annotations

import base64
import hmac
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from starlette.responses import StreamingResponse

from ..substrate import open_db
from ..substrate.objects import object_exists, object_size, read_object

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator

    from starlette.requests import Request
    from starlette.responses import Response

log = structlog.get_logger(__name__)


_TOKEN_RE = re.compile(r"^Bearer\s+(.+)$")


def _unauthorized() -> Response:
    from starlette.responses import JSONResponse

    return JSONResponse(
        {"error": "unauthorized"},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer realm="export"'},
    )


def _check_auth(request: Request) -> bool:
    """Accept the main MCP bearer (``AFAIR_AUTH_TOKEN``) or the optional
    scoped ``AFAIR_EXPORT_TOKEN``. Constant-time compare on both.
    """
    settings = request.app.state.settings
    header = request.headers.get("authorization", "")
    match = _TOKEN_RE.match(header)
    if match is None:
        return False
    presented = match.group(1).strip()

    candidates: list[str] = []
    if settings.auth_token is not None:
        candidates.append(settings.auth_token.get_secret_value())
    if settings.export_token is not None:
        candidates.append(settings.export_token.get_secret_value())
    if not candidates:
        # Neither credential configured — fail closed.
        return False

    # Compare against every candidate so a single accepted token unlocks
    # the endpoint. hmac.compare_digest on each (constant-time) keeps
    # the comparison side-channel safe.
    ok = False
    for c in candidates:
        if hmac.compare_digest(presented, c):
            ok = True
    return ok


def _iter_export(
    vault_dir: Path,
    *,
    include_blobs: bool,
) -> Iterator[str]:
    """Stream JSONL records. Each yield is a single ``…\\n`` line."""
    conn = open_db(vault_dir)
    try:
        # Events first, in chronological order.
        rows = conn.execute(
            """
            SELECT id, content_hash, kind, origin, parent_hashes, schema_version,
                   payload, created_at
            FROM events
            ORDER BY created_at ASC, id ASC
            """,
        )
        for row in rows:
            payload_text = row["payload"]
            try:
                payload = json.loads(payload_text) if payload_text else None
            except json.JSONDecodeError:
                payload = {"_raw": payload_text}
            yield (
                json.dumps(
                    {
                        "kind": "event",
                        "id": row["id"],
                        "content_hash": row["content_hash"],
                        "event_kind": row["kind"],
                        "origin": row["origin"],
                        "parent_hashes": json.loads(row["parent_hashes"] or "null"),
                        "schema_version": row["schema_version"],
                        "payload": payload,
                        "created_at": row["created_at"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )

        # Interpretations next (all versions, in chronological order).
        rows = conn.execute(
            """
            SELECT id, event_id, event_hash, version, produced_at, produced_by, extraction
            FROM interpretations
            ORDER BY produced_at ASC, id ASC
            """,
        )
        for row in rows:
            try:
                extraction = json.loads(row["extraction"]) if row["extraction"] else None
            except json.JSONDecodeError:
                extraction = {"_raw": row["extraction"]}
            yield (
                json.dumps(
                    {
                        "kind": "interpretation",
                        "id": row["id"],
                        "event_id": row["event_id"],
                        "event_hash": row["event_hash"],
                        "version": row["version"],
                        "produced_by": row["produced_by"],
                        "extraction": extraction,
                        "produced_at": row["produced_at"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )

        # Entity graph: entities, mentions, edges, merges, invalidations.
        for table, kind in (
            ("entities", "entity"),
            ("entity_mentions", "entity_mention"),
            ("entity_edges", "entity_edge"),
            ("entity_merges", "entity_merge"),
            ("edge_invalidations", "edge_invalidation"),
        ):
            rows = conn.execute(
                f"SELECT * FROM {table} ORDER BY rowid ASC",
            )
            for row in rows:
                # sqlite3.Row is dict-like via keys() but iteration yields
                # column INDICES, not names — the ruff SIM118 suggestion
                # ("for k in row" instead of "for k in row.keys()") would
                # be wrong here. Use the explicit .keys() API.
                d = {k: row[k] for k in row.keys()}  # noqa: SIM118
                d["kind"] = kind
                yield json.dumps(d, ensure_ascii=False, sort_keys=True, default=str) + "\n"

        # Blobs — referenced by hash; optionally inlined as base64.
        # We iterate the event payloads for blob_hash fields rather
        # than the filesystem directly so the user gets blobs that
        # are actually reachable from event history (not orphans).
        seen_hashes: set[str] = set()
        rows = conn.execute(
            "SELECT payload FROM events WHERE payload LIKE '%blob_hash%'",
        )
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                continue
            for h in _extract_blob_hashes(payload):
                if h in seen_hashes:
                    continue
                seen_hashes.add(h)
                if not object_exists(vault_dir, h):
                    continue
                size = object_size(vault_dir, h)
                rec: dict[str, Any] = {
                    "kind": "blob",
                    "blob_hash": h,
                    "size_bytes": size,
                }
                if include_blobs:
                    rec["content_b64"] = base64.b64encode(
                        read_object(vault_dir, h),
                    ).decode("ascii")
                yield json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n"

        # Manifest as the final record. Lets the importer verify
        # completeness ("did the stream finish?").
        yield (
            json.dumps(
                {
                    "kind": "manifest",
                    "produced_at": datetime.now(UTC).isoformat(),
                    "include_blobs": include_blobs,
                    "format_version": 1,
                    "note": (
                        "Stream terminator. If you didn't see this line, the "
                        "export was truncated mid-stream — retry with the "
                        "same query."
                    ),
                },
                sort_keys=True,
            )
            + "\n"
        )
    finally:
        conn.close()


def _extract_blob_hashes(payload: Any) -> Iterator[str]:
    """Walk a payload structure yielding blob_hash strings encountered."""
    if isinstance(payload, dict):
        h = payload.get("blob_hash")
        if isinstance(h, str) and h.startswith("sha256:"):
            yield h
        for v in payload.values():
            yield from _extract_blob_hashes(v)
    elif isinstance(payload, list):
        for v in payload:
            yield from _extract_blob_hashes(v)


async def export_endpoint(request: Request) -> Response:
    if not _check_auth(request):
        return _unauthorized()

    include_blobs = request.query_params.get("blobs", "") == "inline"
    vault_dir = Path(request.app.state.settings.vault_dir)

    # Wrap the synchronous DB iterator in an async generator. The
    # `for line in _iter_export(...)` runs in the request thread; the
    # StreamingResponse pumps each line out as a chunk. SQLite reads
    # are fast and bounded; no need for thread-pool offloading at the
    # volumes we ship (Phase 0: sub-GB vaults).
    async def _stream() -> AsyncGenerator[bytes, None]:
        for line in _iter_export(vault_dir, include_blobs=include_blobs):
            yield line.encode("utf-8")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    filename = f"afair-export-{stamp}.jsonl"
    log.info("export.started", include_blobs=include_blobs, filename=filename)
    return StreamingResponse(
        _stream(),
        media_type="application/x-ndjson; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Format-Version": "1",
        },
    )
