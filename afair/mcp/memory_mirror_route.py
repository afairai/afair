"""Read-only dashboard projection of current living memory.

The Memory Mirror is a view, not a second store. It reads live synthesis events
and their immutable source records directly from the user's vault. Nothing in
this route edits a source, changes a cluster, or creates a manual filing model.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse

from ..agents.conflict_resolver import flag_is_unresolved, read_conflicts_batch
from ..agents.entity_articles import ENTITY_ARTICLE_KIND
from ..agents.invalidation import INVALIDATE_KIND, read_invalidations_batch
from ..agents.living_syntheses import LIVING_SYNTHESIS_KIND
from ..substrate import open_db, read_event_by_hash
from ..substrate.content_corrections import point_digest, read_key_point_reviews
from .cors import cors_headers
from .internal_auth import authorize_internal

if TYPE_CHECKING:
    from sqlite3 import Connection

    from starlette.requests import Request
    from starlette.responses import Response

DEFAULT_LIMIT = 24
MAX_LIMIT = 100
SOURCE_PREVIEW_CHARS = 320


def _unauthorized(request: Request) -> Response:
    return JSONResponse(
        {"error": "unauthorized"},
        status_code=401,
        headers={
            "WWW-Authenticate": 'Bearer realm="memory-mirror"',
            **cors_headers(request),
        },
    )


def _conn(request: Request) -> Connection:
    return open_db(Path(request.app.state.settings.vault_dir))


async def memory_mirror_endpoint(request: Request) -> Response:
    if not authorize_internal(request):
        return _unauthorized(request)

    try:
        requested = int(request.query_params.get("limit", str(DEFAULT_LIMIT)))
    except ValueError:
        requested = DEFAULT_LIMIT
    limit = min(MAX_LIMIT, max(1, requested))

    conn = _conn(request)
    try:
        syntheses = _read_syntheses(conn, limit=limit)
    finally:
        conn.close()

    return JSONResponse(
        {
            "generated_at": datetime.now(UTC).isoformat(),
            "stats": {
                "current_syntheses": len(syntheses),
                "thin_evidence": sum(bool(item["thin_evidence"]) for item in syntheses),
                "unresolved_conflicts": sum(
                    int(item["unresolved_conflict_count"]) for item in syntheses
                ),
                "stale_sources": sum(int(item["stale_source_count"]) for item in syntheses),
            },
            "syntheses": syntheses,
        },
        headers={"Cache-Control": "no-store", **cors_headers(request)},
    )


def _read_syntheses(conn: Connection, *, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT e.id, e.content_hash, e.created_at, e.kind, e.payload
        FROM events e
        WHERE e.kind IN (?, ?)
          AND NOT EXISTS (
            SELECT 1 FROM events inv
            WHERE inv.kind = ?
              AND json_extract(inv.payload, '$.target_hash') = e.content_hash
          )
        ORDER BY CASE WHEN e.kind = ? THEN 0 ELSE 1 END,
                 e.created_at DESC, e.id DESC
        LIMIT ?
        """,
        (
            LIVING_SYNTHESIS_KIND,
            ENTITY_ARTICLE_KIND,
            INVALIDATE_KIND,
            LIVING_SYNTHESIS_KIND,
            limit,
        ),
    ).fetchall()

    # Batch-read the effective key-point suppression verdict per served
    # synthesis (Flavor B-b2), resolved across both the exact-hash lane and the
    # cluster-fallback lane so a suppression carries forward to a re-derived
    # synthesis of the same cluster (a re-synthesis is a NEW event with a NEW
    # content_hash). Projection-only: the synthesis payload is NEVER rewritten
    # (I2); we annotate served key points with suppressed:true + a caveat so a
    # marked-wrong point is served WITH a marker, not dropped (ADR-0004).
    synthesis_clusters: dict[str, str | None] = {}
    for row in rows:
        if row["kind"] != LIVING_SYNTHESIS_KIND:
            continue
        try:
            row_payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            row_payload = {}
        cluster = row_payload.get("cluster_id")
        synthesis_clusters[row["content_hash"]] = cluster if isinstance(cluster, str) else None
    reviews_by_synthesis = read_key_point_reviews(conn, synthesis_clusters)

    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        citations = _string_list(payload.get("citations"))
        source_invalidations = read_invalidations_batch(conn, citations)
        conflicts = read_conflicts_batch(conn, citations)
        sources = []
        unresolved_count = 0
        for source_hash in citations:
            source = read_event_by_hash(conn, source_hash)
            if source is None:
                sources.append(
                    {
                        "content_hash": source_hash,
                        "missing": True,
                        "current": False,
                    }
                )
                continue
            # Serve ALL conflict flags (resolved ones carry their resolution, so
            # the dashboard can show a "resolved by you" badge — ADR-0004
            # caveat-not-suppress), but only count the still-unresolved ones
            # toward the tension total (ADR-0008).
            all_conflicts = conflicts.get(source_hash, [])
            unresolved_count += sum(1 for flag in all_conflicts if flag_is_unresolved(flag))
            sources.append(
                {
                    "event_id": source.id,
                    "content_hash": source.content_hash,
                    "created_at": source.created_at,
                    "kind": source.kind,
                    "preview": _source_preview(source.payload),
                    "current": source_hash not in source_invalidations,
                    "missing": False,
                    "conflicts": all_conflicts,
                }
            )

        stale_count = sum(not source["current"] for source in sources)
        living = row["kind"] == LIVING_SYNTHESIS_KIND
        key_points = payload.get("key_points", [])
        if not isinstance(key_points, list):
            key_points = []
        if living:
            key_points = _annotate_key_points(
                key_points, reviews_by_synthesis.get(row["content_hash"], {})
            )
        out.append(
            {
                "event_id": row["id"],
                "content_hash": row["content_hash"],
                "created_at": row["created_at"],
                "format": "living_synthesis" if living else "legacy_entity_article",
                "cluster_id": payload.get("cluster_id") if living else None,
                "title": payload.get("title")
                or payload.get("canonical_name")
                or "Untitled synthesis",
                "summary": payload.get("text", ""),
                "signals": _string_list(payload.get("signals")),
                "key_points": key_points,
                "open_questions": _string_list(payload.get("open_questions")),
                "evidence_count": len(citations),
                "citation_coverage": float(payload.get("citation_coverage", 0.0)),
                "thin_evidence": bool(payload.get("thin_evidence", len(citations) < 4)),
                "ancestor_cluster_ids": _string_list(payload.get("ancestor_cluster_ids")),
                "previous_synthesis_hashes": _string_list(payload.get("previous_synthesis_hashes")),
                "changed": bool(payload.get("previous_synthesis_hashes")),
                "stale_source_count": stale_count,
                "unresolved_conflict_count": unresolved_count,
                "sources": sources,
            }
        )
    return out


def _annotate_key_points(
    key_points: list[Any], reviews: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Annotate served key points with the operator's suppression verdict.

    Projection-only (I2): the synthesis payload is untouched; each served point
    gains ``suppressed`` + (when suppressed) a ``suppression`` block. ``reviews``
    is the already-resolved ``{point_digest: {verdict, note, decided_at, ...}}``
    for THIS synthesis from :func:`read_key_point_reviews`, which has already
    applied the exact-over-cluster precedence and the cluster-fallback carry
    across re-synthesis. Matched by ``point_digest`` of the served text, so a
    verbatim key point that re-forms on a re-synthesis of the same cluster
    carries the verdict forward (a reworded point produces a different digest and
    misses — documented b3 limitation). A ``restore`` verdict is served as
    ``suppressed: false`` (the latest-wins row already reflects it).
    """
    annotated: list[dict[str, Any]] = []
    for item in key_points:
        if not isinstance(item, dict):
            annotated.append(item)
            continue
        point = item.get("point")
        enriched = dict(item)
        review = reviews.get(point_digest(point)) if isinstance(point, str) else None
        suppressed = review is not None and review.get("verdict") == "suppress"
        enriched["suppressed"] = suppressed
        if suppressed and review is not None:
            enriched["suppression"] = {
                "note": review.get("note"),
                "decided_at": review.get("decided_at"),
            }
        annotated.append(enriched)
    return annotated


def _source_preview(payload: dict[str, Any]) -> str:
    value = payload.get("text") or payload.get("context") or payload.get("result") or ""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    cleaned = " ".join(value.split())
    return cleaned[:SOURCE_PREVIEW_CHARS]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]
