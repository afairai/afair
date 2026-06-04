"""User-mintable bearer tokens for agents / CI / per-bot scoping.

The user receives ONE master bearer in their onboarding mail
(``AFAIR_AUTH_TOKEN``). With this module they can mint additional
tokens for individual agents and revoke them independently — same
substrate, same vault, but credentials they can rotate without
losing the master.

Storage
-------
``api_tokens`` table in ``substrate.db`` (encrypted at rest by
SQLCipher). Plaintext NEVER touches the disk — only the sha256 hash
does. The plaintext is returned exactly once by ``mint`` and never
again recoverable. Lost a token? Mint a new one.

Auth precedence (handled in ``afair.mcp.auth``):
  1. Static ``AFAIR_AUTH_TOKEN`` (the master).
  2. Any non-revoked row in ``api_tokens`` whose hash matches the
     presented bearer.
  3. JWT issued via OAuth.

Each successful match bumps ``last_used_at`` so the user can see
which of their tokens are dormant + safe to revoke.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from ulid import ULID

# 32 random bytes → ~43 char urlsafe-b64. Matches secrets.token_urlsafe(32)
# which is what AFAIR_AUTH_TOKEN uses, so all tokens look ~the same.
TOKEN_BYTES = 32
TOKEN_PREFIX = "afair_tok__"

Scope = Literal["full", "read", "write"]


@dataclass(frozen=True)
class ApiToken:
    id: str
    label: str
    scope: Scope
    created_at: str
    last_used_at: str | None
    revoked: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ApiToken:
        return cls(
            id=row["id"],
            label=row["label"],
            scope=row["scope"],
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            revoked=row["revoked_at"] is not None,
        )


@dataclass(frozen=True)
class MintedToken:
    """Returned from ``mint()`` exactly once. Caller MUST hand the
    plaintext to the user immediately and not persist it server-side.
    """

    id: str
    label: str
    scope: Scope
    plaintext: str
    created_at: str


def _hash_token(plaintext: str) -> str:
    """sha256 hex. Same hash function as the rest of the substrate
    (oauth_codes, etc.) — single primitive across the codebase."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def mint(conn: sqlite3.Connection, *, label: str, scope: Scope = "full") -> MintedToken:
    """Mint a new token. Returns the plaintext value exactly once."""
    if not label or len(label) > 80:
        raise ValueError("label must be 1..80 chars")
    if scope not in ("full", "read", "write"):
        raise ValueError(f"unknown scope: {scope}")

    token_id = f"tok_{ULID()}"
    plaintext = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
    token_hash = _hash_token(plaintext)
    created_at = _now_iso()

    conn.execute(
        """INSERT INTO api_tokens (id, label, token_hash, scope, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (token_id, label.strip(), token_hash, scope, created_at),
    )
    conn.commit()
    return MintedToken(
        id=token_id,
        label=label.strip(),
        scope=scope,
        plaintext=plaintext,
        created_at=created_at,
    )


def list_all(conn: sqlite3.Connection) -> list[ApiToken]:
    """Return every token (including revoked) ordered newest first."""
    rows = conn.execute(
        """SELECT id, label, scope, created_at, last_used_at, revoked_at
           FROM api_tokens
           ORDER BY created_at DESC""",
    ).fetchall()
    return [ApiToken.from_row(r) for r in rows]


def revoke(conn: sqlite3.Connection, token_id: str) -> bool:
    """Mark a token revoked. Returns True if it actually flipped from
    active to revoked; False if it was already revoked or not found.
    Idempotent: re-revoking a revoked token is a no-op."""
    cur = conn.execute(
        """UPDATE api_tokens
           SET revoked_at = ?
           WHERE id = ? AND revoked_at IS NULL""",
        (_now_iso(), token_id),
    )
    conn.commit()
    return cur.rowcount > 0


def verify(conn: sqlite3.Connection, presented: str) -> ApiToken | None:
    """Look up a presented bearer in the api_tokens table.

    Constant-time semantics: we compute the hash and look it up by
    index; the DB lookup does not branch on whether the row exists,
    and we never compare hashes with ``==`` against an attacker-
    controlled string. Caller has already done the static-bearer
    short-circuit, so a miss here just falls through to JWT auth.

    Bumps ``last_used_at`` on a successful match. Revoked rows are
    treated as misses.
    """
    if not presented:
        return None
    presented_hash = _hash_token(presented)
    row = conn.execute(
        """SELECT id, label, scope, created_at, last_used_at, revoked_at, token_hash
           FROM api_tokens
           WHERE token_hash = ?
           LIMIT 1""",
        (presented_hash,),
    ).fetchone()
    if row is None:
        return None
    # Belt and suspenders: only treat as a hit if the hash matches
    # constant-time (the WHERE already filtered by hash equality, but
    # this guards against future row reuse / migration drift).
    if not hmac.compare_digest(row["token_hash"], presented_hash):
        return None
    if row["revoked_at"] is not None:
        return None
    # Bump last_used_at. Best-effort: a failed write is non-fatal.
    try:
        conn.execute(
            "UPDATE api_tokens SET last_used_at = ? WHERE id = ?",
            (_now_iso(), row["id"]),
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    return ApiToken.from_row(row)
