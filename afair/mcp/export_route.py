"""Vault export endpoint.

Streams the whole vault out as JSON Lines. One line per record,
records ordered by ``created_at`` ascending so a downstream reader
can replay the timeline in order without sorting.

Record shape:
  {"kind": "event",         ...event row + parsed payload}
  {"kind": "interpretation", ...interpretation row with extraction JSON}
  {"kind": "entity",        ...canonical entity (its own kind column as "entity_kind")}
  {"kind": "entity_mention", ...mention linking an event to an entity}
  {"kind": "entity_edge",   ...directed edge between two entities}
  {"kind": "edge_confidence_score", ...append-only served-confidence overlay row}
  {"kind": "entity_merge",  ...merge decision between two entities}
  {"kind": "edge_invalidation",  ...an edge withdrawn from the live graph}
  {"kind": "merge_invalidation", ...a merge undone (rejected) by the operator}
  {"kind": "entity_retraction",  ...an entity withdrawn as noise}
  {"kind": "edge_review",   ...operator confirm/reject verdict on an edge}
  {"kind": "entity_identity", ...v2 name-first identity ledger row}
  {"kind": "entity_kind_assignment", ...append-only retype of an entity}
  {"kind": "kind_registry", ...one registered ontology kind}
  {"kind": "kind_revision", ...one ontology revision (add/rename/merge/...)}
  {"kind": "kind_observation", ...raw extractor kind proposal, preserved}
  {"kind": "proposed_correction", ...entity-audit proposal + its decision}
  {"kind": "proposed_ontology_revision", ...schema-evolver proposal + decision}
  {"kind": "tuner_state",   ...self-improvement promote/rollback/… record (I7)}

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
from ..substrate.blob_gc import blob_hashes_in_payload
from ..substrate.objects import (
    object_exists,
    object_plaintext_size,
    read_object,
)
from .cors import cors_headers

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator

    from starlette.requests import Request
    from starlette.responses import Response

log = structlog.get_logger(__name__)


_TOKEN_RE = re.compile(r"^Bearer\s+(.+)$")


def _unauthorized(request: Request) -> Response:
    from starlette.responses import JSONResponse

    # CORS on the 401 too, so the browser surfaces the real status to the
    # dashboard instead of an opaque CORS error when the token is wrong.
    return JSONResponse(
        {"error": "unauthorized"},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer realm="export"', **cors_headers(request)},
    )


def _check_auth(request: Request) -> str | None:
    """Authenticate the export request.

    Accepts the main MCP bearer (``AFAIR_AUTH_TOKEN``) or the optional
    scoped ``AFAIR_EXPORT_TOKEN``. Constant-time compare on both. Returns a
    label naming WHICH credential matched ("master" | "export") so the
    caller can audit-log the full-vault dump, or None on failure.

    Both credentials always run a compare even after a match, so the total
    work is constant regardless of which (or neither) matched — no early-out
    timing signal about credential validity.
    """
    settings = request.app.state.settings
    header = request.headers.get("authorization", "")
    match = _TOKEN_RE.match(header)
    if match is None:
        return None
    presented = match.group(1).strip()

    matched: str | None = None
    if settings.auth_token is not None and hmac.compare_digest(
        presented, settings.auth_token.get_secret_value()
    ):
        matched = "master"
    if settings.export_token is not None and hmac.compare_digest(
        presented, settings.export_token.get_secret_value()
    ):
        matched = matched or "export"
    return matched


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

        # Entity graph + correction ledger + ontology (ADR-0002 / ADR-0003).
        #
        # Everything below is non-regenerable substrate-of-record: operator
        # and agent decisions (retractions, rejected merges, edge verdicts,
        # kind registry/revisions/assignments, v2 identity ordinals, raw
        # kind proposals, tuner self-modifications) that no rebuild path
        # (scripts/rebuild_vault.py, scripts/backfill_entities.py) can
        # reproduce from events. Omitting any of them would make an
        # export → import round-trip resurrect deleted/retyped entities or
        # lose the ontology — a violation of I4 (the export must be a
        # COMPLETE record of the substrate the user owns).
        #
        # Stream order is FK-safe by construction: events precede
        # everything; entities precede their dependents (mentions, edges,
        # merges, retractions, identities, kind assignments, observations,
        # corrections); entity_merges precede merge_invalidations;
        # entity_edges precede edge_confidence_scores, edge_invalidations and
        # edge_reviews; kind_registry precedes kind_revisions. An importer that
        # inserts in stream order never sees a dangling reference.
        #
        # edge_confidence_scores is technically re-derivable by the cold-path
        # scorer, but the rows are small and the belief history ("what did the
        # vault believe about this edge, and why") is part of what the user
        # owns (I4 completeness), so including it costs nothing.
        #
        # Deliberately EXCLUDED, each with a reason:
        #   events_fts / events_vec — derived search indexes, rebuilt from
        #     events by the running system;
        #   event_temporal — re-derived by the temporal worker from events
        #     (idempotent on UNIQUE(event_hash, computed_by));
        #   pipeline_events — lifecycle diagnostics about the pipeline, not
        #     memory; one row per stage per event would dwarf the export;
        #   oauth_* / api_tokens / export_jobs — host-local credential and
        #     job state: exporting token hashes into a plaintext dump would
        #     widen the credential blast radius, and they mean nothing on
        #     the machine an export is restored to.
        #
        # proposed_corrections / proposed_ontology_revisions ARE included
        # even though they are mutable suggestion queues: their DECIDED rows
        # (confirmed/rejected, with decided_by/decided_at) are operator
        # verdicts recorded nowhere else — losing a 'rejected' row would let
        # a re-run of the audit / evolver re-propose (and, for auto-tier
        # corrections, re-apply) something the operator already refused.
        # Pending rows are regenerable, but carrying them is harmless.
        for table, kind in (
            ("entities", "entity"),
            ("entity_mentions", "entity_mention"),
            ("entity_edges", "entity_edge"),
            ("edge_confidence_scores", "edge_confidence_score"),
            ("entity_merges", "entity_merge"),
            ("edge_invalidations", "edge_invalidation"),
            ("merge_invalidations", "merge_invalidation"),
            ("entity_retractions", "entity_retraction"),
            ("edge_reviews", "edge_review"),
            ("entity_identities", "entity_identity"),
            ("entity_kind_assignments", "entity_kind_assignment"),
            ("kind_registry", "kind_registry"),
            ("kind_revisions", "kind_revision"),
            ("kind_observations", "kind_observation"),
            ("proposed_corrections", "proposed_correction"),
            ("proposed_ontology_revisions", "proposed_ontology_revision"),
            ("tuner_state", "tuner_state"),
            # ADR-0006: the client-provenance sidecar is the user's own record of
            # which AI tool wrote each event — part of the substrate they own, so
            # it must survive an export → import round-trip (I4). References
            # events(id), which precede it in the stream, so FK order holds.
            ("event_provenance", "event_provenance"),
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
                # A table's own ``kind`` column (entities.kind — the entity's
                # type; proposed_corrections.kind; tuner_state.kind) must not
                # be clobbered by the record discriminator. Re-key it to
                # ``<record_kind>_kind`` ("entity_kind", ...). Purely
                # additive for consumers: the old export silently DESTROYED
                # the column, so nothing could have depended on it.
                if "kind" in d:
                    d[f"{kind}_kind"] = d.pop("kind")
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
            for h in blob_hashes_in_payload(payload):
                if h in seen_hashes:
                    continue
                seen_hashes.add(h)
                if not object_exists(vault_dir, h):
                    continue
                # Plaintext size so it matches the decrypted bytes in
                # content_b64 below — a consumer that base64-decodes and
                # length-checks must not see a 32-byte envelope discrepancy.
                size = object_plaintext_size(vault_dir, h)
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


async def export_endpoint(request: Request) -> Response:
    credential = _check_auth(request)
    if credential is None:
        return _unauthorized(request)

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
    # Audit the full-vault dump: a leaked master bearer streaming the whole
    # encrypted-at-rest vault as plaintext is the worst-case blast radius, so
    # every export is attributable — which credential, which IP, which
    # request id. (Security: export blast-radius visibility.)
    fly_ip = request.headers.get("fly-client-ip") or (
        request.client.host if request.client else None
    )
    log.info(
        "export.started",
        include_blobs=include_blobs,
        filename=filename,
        credential=credential,
        client_ip=fly_ip,
    )
    return StreamingResponse(
        _stream(),
        media_type="application/x-ndjson; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Format-Version": "1",
            # Cross-origin so the /account dashboard (afair.ai) can fetch the
            # stream with the master bearer and trigger a browser download.
            # Expose Content-Disposition so the client JS can read the
            # server-suggested filename off the response.
            **cors_headers(request),
            "Access-Control-Expose-Headers": "Content-Disposition, X-Format-Version",
        },
    )
