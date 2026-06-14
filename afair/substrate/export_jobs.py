"""Async vault-export job state.

The export endpoint streams the whole vault synchronously, which is fine for
a quick look but wrong for a large/blob-heavy vault behind a browser: it
holds the bytes in memory and a closed tab loses the download. So a request
spawns a background job that materializes a gzip'd, encrypted JSONL snapshot
on the per-user volume; the user is emailed a capability link and the
dashboard polls for readiness.

This module is the state layer over the MUTABLE ``export_jobs`` table (no
append-only triggers — a job legitimately transitions pending → ready →
downloaded, and is purged after its TTL). The artifact itself lives at
``<vault_dir>/exports/<id>.bin`` (gzip then AES-GCM with the vault key);
this table is the index + the capability-token gate.

Security: the download token's sha256 is stored, never the plaintext — the
plaintext travels only in the emailed link. The artifact is a
plaintext-equivalent dump of the whole vault, so it is encrypted at rest
and auto-purged after ``expires_at``.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

DEFAULT_RETENTION_HOURS = 72
"""How long a ready artifact + its download link live before auto-purge."""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def hash_token(token: str) -> str:
    """sha256 hex of a download token — what we store + compare against."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExportJob:
    id: str
    status: str
    include_blobs: bool
    artifact_filename: str | None
    size_bytes: int | None
    error: str | None
    requested_at: str
    ready_at: str | None
    expires_at: str | None
    downloaded_at: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ExportJob:
        return cls(
            id=row["id"],
            status=row["status"],
            include_blobs=bool(row["include_blobs"]),
            artifact_filename=row["artifact_filename"],
            size_bytes=row["size_bytes"],
            error=row["error"],
            requested_at=row["requested_at"],
            ready_at=row["ready_at"],
            expires_at=row["expires_at"],
            downloaded_at=row["downloaded_at"],
        )


def create_job(
    conn: sqlite3.Connection,
    *,
    include_blobs: bool = True,
    retention_hours: int = DEFAULT_RETENTION_HOURS,
) -> tuple[str, str]:
    """Create a pending job. Returns ``(job_id, download_token_plain)``.

    The plaintext token is returned ONCE here so the caller can build the
    download link; only its hash is persisted. ``expires_at`` is set now so
    the link's lifetime is fixed from request time regardless of how long
    generation takes (a slow export does not extend the exposure window
    beyond a few extra minutes).
    """
    job_id = f"exp_{secrets.token_hex(12)}"
    token = secrets.token_urlsafe(32)
    now = _now()
    expires = now + timedelta(hours=retention_hours)
    conn.execute(
        """
        INSERT INTO export_jobs (
            id, status, include_blobs, download_token_hash,
            requested_at, expires_at
        ) VALUES (?, 'pending', ?, ?, ?, ?)
        """,
        (job_id, 1 if include_blobs else 0, hash_token(token), _iso(now), _iso(expires)),
    )
    conn.commit()
    return job_id, token


def mark_ready(
    conn: sqlite3.Connection, job_id: str, *, artifact_filename: str, size_bytes: int
) -> None:
    conn.execute(
        """
        UPDATE export_jobs
        SET status = 'ready', artifact_filename = ?, size_bytes = ?, ready_at = ?
        WHERE id = ? AND status = 'pending'
        """,
        (artifact_filename, size_bytes, _iso(_now()), job_id),
    )
    conn.commit()


def mark_failed(conn: sqlite3.Connection, job_id: str, *, error: str) -> None:
    conn.execute(
        "UPDATE export_jobs SET status = 'failed', error = ? WHERE id = ? AND status = 'pending'",
        (error[:2000], job_id),
    )
    conn.commit()


def mark_downloaded(conn: sqlite3.Connection, job_id: str) -> None:
    """Stamp the first successful download. Not single-use — the link works
    until expiry (72h) so a flaky connection can retry — but we record the
    first fetch for the audit trail."""
    conn.execute(
        "UPDATE export_jobs SET downloaded_at = COALESCE(downloaded_at, ?) WHERE id = ?",
        (_iso(_now()), job_id),
    )
    conn.commit()


def latest_job(conn: sqlite3.Connection) -> ExportJob | None:
    """The most recently requested job — what the dashboard status reflects."""
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM export_jobs ORDER BY requested_at DESC LIMIT 1",
    ).fetchone()
    return ExportJob.from_row(row) if row else None


def job_by_token_hash(conn: sqlite3.Connection, token_hash: str) -> ExportJob | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM export_jobs WHERE download_token_hash = ? LIMIT 1",
        (token_hash,),
    ).fetchone()
    return ExportJob.from_row(row) if row else None


def has_active_pending(conn: sqlite3.Connection) -> ExportJob | None:
    """Return a still-pending job if one exists, so a double-click doesn't
    spawn two generations."""
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM export_jobs WHERE status = 'pending' ORDER BY requested_at DESC LIMIT 1",
    ).fetchone()
    return ExportJob.from_row(row) if row else None


def expired_ready_jobs(conn: sqlite3.Connection) -> list[ExportJob]:
    """Ready jobs whose link has expired — their artifacts must be purged."""
    conn.row_factory = sqlite3.Row
    now = _iso(_now())
    rows = conn.execute(
        "SELECT * FROM export_jobs WHERE status = 'ready' AND expires_at < ?",
        (now,),
    ).fetchall()
    return [ExportJob.from_row(r) for r in rows]


def mark_expired(conn: sqlite3.Connection, job_id: str) -> None:
    conn.execute(
        "UPDATE export_jobs SET status = 'expired' WHERE id = ?",
        (job_id,),
    )
    conn.commit()
