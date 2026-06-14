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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

DEFAULT_RETENTION_HOURS = 72
"""How long a ready artifact + its download link live before auto-purge."""

PENDING_DEAD_AFTER_MINUTES = 30
"""A job still 'pending' after this long is presumed dead (the worker was
OOM-killed / crashed before it could mark_failed). The purge sweep flips it
to 'failed', and has_active_pending ignores it so a new request isn't blocked
forever by a stuck row."""


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
) -> int:
    """Flip a pending job to ready. Returns rows touched (0 = the job was no
    longer pending — a lost race; the caller must clean up the artifact it
    just wrote and NOT notify, or the user gets a dead link)."""
    cur = conn.execute(
        """
        UPDATE export_jobs
        SET status = 'ready', artifact_filename = ?, size_bytes = ?, ready_at = ?
        WHERE id = ? AND status = 'pending'
        """,
        (artifact_filename, size_bytes, _iso(_now()), job_id),
    )
    conn.commit()
    return cur.rowcount


def mark_failed(conn: sqlite3.Connection, job_id: str, *, error: str) -> int:
    cur = conn.execute(
        "UPDATE export_jobs SET status = 'failed', error = ? WHERE id = ? AND status = 'pending'",
        (error[:2000], job_id),
    )
    conn.commit()
    return cur.rowcount


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
    row = conn.execute(
        "SELECT * FROM export_jobs ORDER BY requested_at DESC LIMIT 1",
    ).fetchone()
    return ExportJob.from_row(row) if row else None


def job_by_id(conn: sqlite3.Connection, job_id: str) -> ExportJob | None:
    """Fetch a specific job — the runner reports on ITS job, not 'latest'
    (which is the wrong key when two requests race)."""
    row = conn.execute("SELECT * FROM export_jobs WHERE id = ? LIMIT 1", (job_id,)).fetchone()
    return ExportJob.from_row(row) if row else None


def job_by_token_hash(conn: sqlite3.Connection, token_hash: str) -> ExportJob | None:
    row = conn.execute(
        "SELECT * FROM export_jobs WHERE download_token_hash = ? LIMIT 1",
        (token_hash,),
    ).fetchone()
    return ExportJob.from_row(row) if row else None


def has_active_pending(conn: sqlite3.Connection) -> ExportJob | None:
    """Return a FRESH still-pending job if one exists, so a double-click
    doesn't spawn two generations. A job pending longer than
    PENDING_DEAD_AFTER_MINUTES is presumed dead (worker crashed/OOM) and does
    NOT block — otherwise one stuck row bricks the feature forever."""
    cutoff = _iso(_now() - timedelta(minutes=PENDING_DEAD_AFTER_MINUTES))
    row = conn.execute(
        "SELECT * FROM export_jobs WHERE status = 'pending' AND requested_at >= ? "
        "ORDER BY requested_at DESC LIMIT 1",
        (cutoff,),
    ).fetchone()
    return ExportJob.from_row(row) if row else None


def stale_pending_jobs(conn: sqlite3.Connection) -> list[ExportJob]:
    """Pending jobs older than the dead-ceiling — the purge sweep fails them."""
    cutoff = _iso(_now() - timedelta(minutes=PENDING_DEAD_AFTER_MINUTES))
    rows = conn.execute(
        "SELECT * FROM export_jobs WHERE status = 'pending' AND requested_at < ?",
        (cutoff,),
    ).fetchall()
    return [ExportJob.from_row(r) for r in rows]


def fail_if_pending(conn: sqlite3.Connection, job_id: str, *, error: str) -> None:
    conn.execute(
        "UPDATE export_jobs SET status = 'failed', error = ? WHERE id = ? AND status = 'pending'",
        (error[:2000], job_id),
    )
    conn.commit()


def expired_ready_jobs(conn: sqlite3.Connection) -> list[ExportJob]:
    """Ready jobs whose link has expired — their artifacts must be purged."""
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
