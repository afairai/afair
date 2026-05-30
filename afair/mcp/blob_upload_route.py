"""Streaming blob-upload endpoint — large binaries without RAM spike.

The MCP ``remember`` tool accepts up to 10 MB of base64-encoded binary
in the JSON body. uvicorn buffers the whole body in RAM before our
handler sees it, so the practical ceiling — not the 10 MB Pydantic cap
itself but the platform RAM cap — limits real-world file sizes.

This endpoint side-steps that constraint. The client opens an HTTP POST
with ``Content-Type: application/octet-stream`` and STREAMS bytes;
afair pipes ``request.stream()`` directly into a
:class:`StreamingObjectWriter` that hashes and writes chunk-by-chunk
to a temp file in the object store, then atomic-renames to the
content-addressed location.

After the upload completes, the client calls ``remember`` with
``content={"type": "blob-ref", "blob_hash": "<sha256:…>", "mime": …,
"filename_hint": …}`` to wire an event row to the already-stored bytes.

Auth: this is an MCP-internal route. The regular ``BearerOrJwtMiddleware``
validates before we ever run. Per-IP rate limit is irrelevant here
(the user's own client legitimately uploads many blobs in a row); the
substrate-level per-identity bucket already applies.

Size cap defaults to 1 GB so a single binary can never wedge a Fly
volume on its own. Override via the route mount kwarg if needed.

The endpoint runs OUTSIDE the BodySizeLimitMiddleware's 12 MB cap by
being explicitly listed in that middleware's ``exempt_paths``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from starlette.responses import JSONResponse

from ..substrate.objects import StreamingObjectWriter
from .context import get_context

if TYPE_CHECKING:
    from starlette.requests import Request

log = structlog.get_logger(__name__)


# Hard cap — 1 GB by default. Single binary can never exceed this even
# if the platform body-size middleware is somehow bypassed. Override
# via env in deployments that genuinely need larger blobs (video).
DEFAULT_MAX_BLOB_UPLOAD_BYTES = 1024 * 1024 * 1024  # 1 GB


async def blob_upload_endpoint(request: Request) -> JSONResponse:
    """POST /internal/blob/upload — streaming chunk-by-chunk.

    Auth is enforced by the upstream BearerOrJwtMiddleware. Request
    must use POST and must NOT use ``application/json`` (we read raw
    bytes; JSON-shaped bodies belong on the regular ``remember`` tool).

    Response: ``{"blob_hash": "sha256:…", "size_bytes": N}`` on success.
    """
    max_bytes = DEFAULT_MAX_BLOB_UPLOAD_BYTES
    ctx = get_context()

    mime = request.headers.get("content-type", "")
    if mime.lower().startswith("application/json"):
        return JSONResponse(
            {
                "error": "invalid_content_type",
                "detail": (
                    "use a binary content-type (application/octet-stream, image/*, "
                    "audio/*, application/pdf, ...) — JSON bodies belong on the "
                    "regular `remember` MCP tool"
                ),
            },
            status_code=400,
        )

    # Honor Content-Length up front when the client sets it — avoids
    # streaming gigabytes before discovering the request was over-cap.
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            declared = int(cl)
        except ValueError:
            return JSONResponse({"error": "invalid_content_length"}, status_code=400)
        if declared > max_bytes:
            return JSONResponse(
                {
                    "error": "payload_too_large",
                    "detail": f"declared {declared} bytes; max {max_bytes}",
                },
                status_code=413,
            )

    writer = StreamingObjectWriter(ctx.vault_dir)
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            if writer.size + len(chunk) > max_bytes:
                writer.abort()
                return JSONResponse(
                    {
                        "error": "payload_too_large",
                        "detail": f"streamed bytes exceed {max_bytes}",
                    },
                    status_code=413,
                )
            writer.feed(chunk)
    except Exception as e:
        writer.abort()
        log.warning("blob_upload.failed", error=str(e), exc_type=type(e).__name__)
        return JSONResponse({"error": "internal_error"}, status_code=500)

    try:
        blob_hash = writer.finalize()
    except Exception as e:
        log.warning("blob_upload.finalize_failed", error=str(e))
        return JSONResponse({"error": "finalize_failed"}, status_code=500)

    log.info("blob_upload.stored", blob_hash=blob_hash, size_bytes=writer.size)
    return JSONResponse(
        {"blob_hash": blob_hash, "size_bytes": writer.size},
        status_code=201,
    )
