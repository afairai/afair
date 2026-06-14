"""HTTP routes for the async vault export.

Three endpoints on the per-user machine, all under /internal/export/*:

  POST /internal/export/request   master bearer OR dashboard session → start
                                  a job (or return the in-flight one)
  GET  /internal/export/status    master bearer OR dashboard session → latest
                                  job state for the dashboard poll
  GET  /internal/export/download  ?token=… capability link (from the email or
                                  the dashboard) → stream the artifact

request + status are cross-origin (the afair.ai dashboard calls them with
the user's session credential — see internal_auth), so they carry CORS +
have OPTIONS preflights. download is a plain top-level navigation gated by
the token in the URL — no CORS, no bearer, the token IS the credential.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import structlog
from starlette.responses import JSONResponse, StreamingResponse

from ..substrate import export_jobs, open_db
from . import export_job
from .cors import cors_headers
from .internal_auth import authorize_internal

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from starlette.requests import Request
    from starlette.responses import Response

log = structlog.get_logger(__name__)


def _job_view(job: export_jobs.ExportJob | None) -> dict[str, object]:
    if job is None:
        return {"status": "none"}
    return {
        "status": job.status,
        "size_bytes": job.size_bytes,
        "requested_at": job.requested_at,
        "ready_at": job.ready_at,
        "expires_at": job.expires_at,
        "include_blobs": job.include_blobs,
    }


async def export_request_endpoint(request: Request) -> Response:
    if not authorize_internal(request):
        return JSONResponse(
            {"error": "unauthorized"}, status_code=401, headers=cors_headers(request)
        )
    settings = request.app.state.settings
    vault_dir: Path = settings.vault_dir
    conn = open_db(vault_dir)
    try:
        # Don't spawn a second generation if one is already running.
        pending = export_jobs.has_active_pending(conn)
        if pending is not None:
            return JSONResponse(
                {"job": _job_view(pending), "started": False},
                headers=cors_headers(request),
            )
        include_blobs = True  # the email artifact is a full air-gapped copy
        job_id, token = export_jobs.create_job(
            conn,
            include_blobs=include_blobs,
            retention_hours=settings.export_retention_hours,
        )
        job = export_jobs.latest_job(conn)
    finally:
        conn.close()

    # Generate in a daemon thread so the request returns immediately. The
    # thread opens its own DB connection (SQLite + WAL is fine concurrently).
    threading.Thread(
        target=export_job.run_job,
        args=(settings, job_id),
        kwargs={"include_blobs": include_blobs, "download_token": token},
        daemon=True,
        name=f"export-{job_id}",
    ).start()
    log.info("export.request.started", job_id=job_id)
    # The capability token is returned ONCE here so the dashboard (which
    # holds the master bearer that authorised this) can build the download
    # link the moment the job is ready, without waiting for the email. Only
    # the hash is persisted; a later status poll never re-surfaces it.
    return JSONResponse(
        {"job": _job_view(job), "started": True, "download_token": token},
        headers=cors_headers(request),
    )


async def export_status_endpoint(request: Request) -> Response:
    if not authorize_internal(request):
        return JSONResponse(
            {"error": "unauthorized"}, status_code=401, headers=cors_headers(request)
        )
    settings = request.app.state.settings
    conn = open_db(settings.vault_dir)
    try:
        job = export_jobs.latest_job(conn)
    finally:
        conn.close()
    return JSONResponse({"job": _job_view(job)}, headers=cors_headers(request))


async def export_download_endpoint(request: Request) -> Response:
    """Stream a ready artifact for a valid, unexpired capability token.

    No bearer + no CORS: this is a top-level browser navigation (the link in
    the email / dashboard). The token is the credential; the artifact streams
    with Content-Disposition so the browser saves it.
    """
    settings = request.app.state.settings
    token = request.query_params.get("token", "")
    if not token:
        return JSONResponse({"error": "missing_token"}, status_code=400)

    conn = open_db(settings.vault_dir)
    try:
        job = export_jobs.job_by_token_hash(conn, export_jobs.hash_token(token))
        if job is None or job.status != "ready" or job.artifact_filename is None:
            return JSONResponse({"error": "not_found_or_not_ready"}, status_code=404)
        # Expiry is enforced here too, not just by the purge sweep, so a link
        # used a second after expiry (before the sweep runs) is still rejected.
        from datetime import UTC, datetime

        if job.expires_at and job.expires_at < datetime.now(UTC).isoformat():
            return JSONResponse({"error": "expired"}, status_code=410)

        data = export_job.read_artifact(settings.vault_dir, job.artifact_filename)
        export_jobs.mark_downloaded(conn, job.id)
    except FileNotFoundError:
        return JSONResponse({"error": "artifact_gone"}, status_code=410)
    finally:
        conn.close()

    fname = export_job.download_filename()
    log.info("export.download", job_id=job.id, size_bytes=len(data))

    def _one_shot() -> Iterator[bytes]:
        yield data

    return StreamingResponse(
        _one_shot(),
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Content-Length": str(len(data)),
            "Cache-Control": "no-store",
        },
    )
