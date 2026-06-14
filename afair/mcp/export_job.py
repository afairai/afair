"""Async vault-export job runner.

Runs on the per-user MCP machine. A request spawns ``run_job`` in a
background thread: it materializes the full JSONL export (reusing the same
record stream the synchronous endpoint uses), gzips and encrypts it onto
the volume, flips the job to ready, and pings afair-web so the user gets an
email. Download decrypts + streams; a purge sweep deletes artifacts past
their TTL.

Why encrypt the artifact: it is a plaintext-equivalent dump of the whole
vault. The rest of the vault is encrypted at rest (SQLCipher DB, AES-GCM
blobs), so the export must be too — same vault key, transparently decrypted
only when the user downloads it.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ..substrate import export_jobs, open_db
from ..substrate.encryption import decrypt_blob, encrypt_blob
from ..substrate.objects import _blob_key_or_none
from .export_route import _iter_export

if TYPE_CHECKING:
    import sqlite3

    from ..settings import Settings

log = structlog.get_logger(__name__)


def exports_dir(vault_dir: Path) -> Path:
    return Path(vault_dir) / "exports"


def _artifact_path(vault_dir: Path, filename: str) -> Path:
    # filename is "<job_id>.bin"; job_id is our own token_hex, no traversal
    # risk, but resolve + containment-check anyway (defense in depth).
    base = exports_dir(vault_dir).resolve()
    p = (base / filename).resolve()
    if base not in p.parents:
        msg = "artifact path escapes the exports directory"
        raise ValueError(msg)
    return p


def generate_artifact(vault_dir: Path, job_id: str, *, include_blobs: bool) -> tuple[str, int]:
    """Build the gzip'd + encrypted JSONL artifact. Returns (filename, size).

    The JSONL is streamed straight into a gzip temp FILE (not a BytesIO), so
    the uncompressed export never lives in RAM. Encryption is still one-pass
    AES-GCM over the gzip'd bytes — for a blob-heavy vault that gzip can be
    large, so a future scale fix is framed/streaming encryption; today the
    peak is ~1x the gzip size, down from ~3x the previous BytesIO path.

    On ANY failure every temp + the half-written final is unlinked, so a
    crash mid-generate never leaks a stray file onto the (tight) volume.
    """
    vault_dir = Path(vault_dir)
    out_dir = exports_dir(vault_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{job_id}.bin"
    path = _artifact_path(vault_dir, filename)
    gz_tmp = out_dir / f"{job_id}.gz.tmp"
    bin_tmp = out_dir / f"{job_id}.bin.tmp"
    try:
        # 1. Stream JSONL → gzip directly to a temp file (no full RAM copy).
        with gzip.open(gz_tmp, "wb", compresslevel=6) as gz:
            for line in _iter_export(vault_dir, include_blobs=include_blobs):
                gz.write(line.encode("utf-8"))
        gz_size = gz_tmp.stat().st_size

        # 2. Encrypt one-pass, freeing each buffer as soon as it's consumed.
        blob_key = _blob_key_or_none()
        gz_bytes = gz_tmp.read_bytes()
        on_disk = encrypt_blob(gz_bytes, blob_key) if blob_key is not None else gz_bytes
        del gz_bytes
        bin_tmp.write_bytes(on_disk)
        del on_disk
        bin_tmp.replace(path)
        return filename, gz_size
    except BaseException:
        bin_tmp.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        raise
    finally:
        gz_tmp.unlink(missing_ok=True)


def read_artifact(vault_dir: Path, filename: str) -> bytes:
    """Read + decrypt an artifact back to its gzip'd JSONL bytes."""
    path = _artifact_path(Path(vault_dir), filename)
    raw = path.read_bytes()
    blob_key = _blob_key_or_none()
    if blob_key is not None:
        from ..substrate.encryption import looks_encrypted

        if looks_encrypted(raw[:8]):
            return decrypt_blob(raw, blob_key)
    return raw


def download_filename(stamp: datetime | None = None) -> str:
    s = (stamp or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"afair-export-{s}.jsonl.gz"


def run_job(settings: Settings, job_id: str, *, include_blobs: bool, download_token: str) -> None:
    """Background body: generate, mark ready, fire the email callback.

    Opens its own DB connection (runs in a worker thread). Any failure is
    recorded as a failed job so the dashboard can surface it rather than
    spinning forever.
    """
    vault_dir = Path(settings.vault_dir)
    conn = open_db(vault_dir)
    try:
        filename, size = generate_artifact(vault_dir, job_id, include_blobs=include_blobs)
        touched = export_jobs.mark_ready(conn, job_id, artifact_filename=filename, size_bytes=size)
        if touched == 0:
            # Lost race: the job was already failed/expired (e.g. the stuck-
            # pending watchdog flipped it). The artifact we just wrote is an
            # orphan — unlink it and do NOT email a link for a dead job.
            log.warning("export.job.lost_race", job_id=job_id)
            with contextlib.suppress(OSError, ValueError):
                _artifact_path(vault_dir, filename).unlink(missing_ok=True)
            return
        job = export_jobs.job_by_id(conn, job_id)
        log.info("export.job.ready", job_id=job_id, size_bytes=size)
        _notify_export_ready(settings, download_token=download_token, job=job)
    except Exception as exc:
        log.error("export.job.failed", job_id=job_id, error=str(exc))
        try:
            export_jobs.mark_failed(conn, job_id, error=str(exc))
        except Exception:
            log.error("export.job.mark_failed_failed", job_id=job_id)
    finally:
        conn.close()


def _notify_export_ready(
    settings: Settings, *, download_token: str, job: export_jobs.ExportJob | None
) -> None:
    """POST the download link to afair-web so it emails the user. Best-effort:
    the dashboard poll covers readiness even if this fails."""
    secret = settings.export_ready_callback_secret
    if secret is None:
        log.info("export.notify.skipped_no_secret")
        return
    # The EXACT clerk userId (not the lowercased `allowlist` property — the
    # afair-web lookup is case-sensitive). Single-tenant per I8 → one entry.
    identity = [s.strip() for s in settings.identity_allowlist.split(",") if s.strip()]
    if not identity:
        log.info("export.notify.skipped_no_identity")
        return

    issuer = settings.effective_oauth_issuer.rstrip("/")
    download_url = f"{issuer}/internal/export/download?token={download_token}"
    payload = json.dumps(
        {
            "clerk_user_id": identity[0],
            "download_url": download_url,
            "size_bytes": job.size_bytes if job else None,
            "expires_at": job.expires_at if job else None,
        }
    ).encode("utf-8")
    url = f"{settings.identity_hub_url.rstrip('/')}/api/internal/export-ready"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {secret.get_secret_value()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            log.info("export.notify.sent", status=resp.status)
    except Exception as exc:
        log.warning("export.notify.failed", error=str(exc))


def start_purge_loop(settings: Settings, *, interval_seconds: int = 3600) -> None:
    """Spawn a daemon thread that purges expired export artifacts hourly.

    Cheap (one indexed SELECT + a few unlinks), bounded, and self-healing —
    a failed cycle logs and retries next interval. Started from the server
    boot path alongside the checkpoint loop.
    """
    import threading
    import time

    def _loop() -> None:
        while True:
            time.sleep(interval_seconds)
            try:
                conn = open_db(settings.vault_dir)
                try:
                    purge_expired(Path(settings.vault_dir), conn)
                finally:
                    conn.close()
            except Exception as exc:
                log.warning("export.purge.cycle_failed", error=str(exc))

    threading.Thread(target=_loop, daemon=True, name="export-purge").start()
    log.info("export.purge.loop_started", interval_seconds=interval_seconds)


def purge_expired(vault_dir: Path, conn: sqlite3.Connection) -> int:
    """Delete expired artifacts, fail stuck-pending jobs, and reconcile orphan
    files on disk. Returns the number of artifacts removed. Safe to re-run."""
    vault_dir = Path(vault_dir)
    purged = 0

    # 1. Expired ready artifacts → unlink + mark expired.
    for job in export_jobs.expired_ready_jobs(conn):
        if job.artifact_filename:
            _safe_unlink(vault_dir, job.artifact_filename)
        export_jobs.mark_expired(conn, job.id)
        purged += 1

    # 2. Stuck-pending jobs (worker crashed/OOM before mark_*) → failed, so a
    #    new request isn't blocked and the dashboard stops spinning.
    for job in export_jobs.stale_pending_jobs(conn):
        export_jobs.fail_if_pending(conn, job.id, error="timed out (worker presumed dead)")
        log.warning("export.purge.failed_stale_pending", job_id=job.id)

    # 3. Orphan files: a *.bin whose job is gone/not-ready (lost-race leftover),
    #    or a stale *.tmp from a crashed generate. Belt-and-suspenders against
    #    a full plaintext-equivalent dump lingering on the volume.
    purged += _reconcile_orphans(vault_dir, conn)

    if purged:
        log.info("export.purge.done", purged=purged)
    return purged


def _safe_unlink(vault_dir: Path, filename: str) -> None:
    try:
        _artifact_path(vault_dir, filename).unlink(missing_ok=True)
    except (OSError, ValueError) as exc:
        log.warning("export.purge.unlink_failed", filename=filename, error=str(exc))


def _reconcile_orphans(vault_dir: Path, conn: sqlite3.Connection) -> int:
    """Unlink export files with no live 'ready' row behind them."""
    import time

    out_dir = exports_dir(vault_dir)
    if not out_dir.is_dir():
        return 0
    removed = 0
    now = time.time()
    for entry in out_dir.iterdir():
        if not entry.is_file():
            continue
        name = entry.name
        if name.endswith(".bin"):
            job = export_jobs.job_by_id(conn, name[: -len(".bin")])
            # Only remove a .bin that is genuinely dead. A 'pending' job's
            # .bin can exist transiently — generate_artifact writes the file
            # BEFORE mark_ready commits — so unlinking on 'pending' would race
            # a live generate and delete a good artifact. Stale-pending jobs
            # are flipped to 'failed' in step 2 above, so they're caught on a
            # later pass (or this one, after the flip).
            if job is None or job.status in ("failed", "expired"):
                _safe_unlink(vault_dir, name)
                removed += 1
        elif name.endswith(".tmp"):
            # Leftover from a crashed generate. Only remove if it's not from an
            # in-flight job (older than the dead-ceiling) to avoid racing a
            # live generate writing right now.
            try:
                age_min = (now - entry.stat().st_mtime) / 60
            except OSError:
                continue
            if age_min > export_jobs.PENDING_DEAD_AFTER_MINUTES:
                entry.unlink(missing_ok=True)
                removed += 1
    return removed
