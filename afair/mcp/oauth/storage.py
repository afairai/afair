"""SQLite-backed storage for OAuth state.

Three logical tables, all in substrate.db alongside the immutable
substrate (backup-locality):
  - oauth_clients         — registered OAuth clients (via DCR)
  - oauth_codes           — one-shot authorization codes (~10 min TTL)
  - oauth_refresh_tokens  — long-lived refresh tokens (hashed at rest)
  - oauth_login_state     — in-flight identity-backend dance state

All are MUTABLE — the events table's append-only triggers do NOT apply
to these.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence


# ── helpers ─────────────────────────────────────────────────────────────────


def _iso(ts: int | float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _hash_token(token: str) -> str:
    """sha256 of a token — stored at rest so a DB leak doesn't expose tokens."""
    return f"sha256:{hashlib.sha256(token.encode('utf-8')).hexdigest()}"


# ── oauth_clients (DCR registrations) ──────────────────────────────────────


@dataclass(frozen=True)
class Client:
    client_id: str
    redirect_uris: tuple[str, ...]
    client_name: str | None
    registered_at: str
    has_secret: bool


def register_client(
    conn: sqlite3.Connection,
    *,
    redirect_uris: Sequence[str],
    client_name: str | None = None,
    confidential: bool = False,
    metadata: dict[str, object] | None = None,
) -> tuple[Client, str | None]:
    """Register a new OAuth client per RFC 7591 (Dynamic Client Registration).

    Returns the client record and (if confidential) the generated secret
    in plaintext — caller must give it back to the client; we only store
    a sha256 hash of it.
    """
    client_id = f"nf_{secrets.token_urlsafe(16)}"
    secret_plain: str | None = None
    secret_hash: str | None = None
    if confidential:
        secret_plain = secrets.token_urlsafe(32)
        secret_hash = _hash_token(secret_plain)

    with conn:
        conn.execute(
            """
            INSERT INTO oauth_clients (
                client_id, client_secret_hash, redirect_uris, client_name,
                registered_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                client_id,
                secret_hash,
                json.dumps(list(redirect_uris)),
                client_name,
                _now_iso(),
                json.dumps(metadata or {}),
            ),
        )

    return (
        Client(
            client_id=client_id,
            redirect_uris=tuple(redirect_uris),
            client_name=client_name,
            registered_at=_now_iso(),
            has_secret=confidential,
        ),
        secret_plain,
    )


def get_client(conn: sqlite3.Connection, client_id: str) -> Client | None:
    row = conn.execute(
        "SELECT * FROM oauth_clients WHERE client_id = ?",
        (client_id,),
    ).fetchone()
    if row is None:
        return None
    return Client(
        client_id=row["client_id"],
        redirect_uris=tuple(json.loads(row["redirect_uris"])),
        client_name=row["client_name"],
        registered_at=row["registered_at"],
        has_secret=row["client_secret_hash"] is not None,
    )


def verify_client_secret(conn: sqlite3.Connection, client_id: str, secret: str) -> bool:
    """Check the client_secret presented at /token against the stored hash."""
    import hmac

    row = conn.execute(
        "SELECT client_secret_hash FROM oauth_clients WHERE client_id = ?",
        (client_id,),
    ).fetchone()
    if row is None or row["client_secret_hash"] is None:
        return False
    return hmac.compare_digest(row["client_secret_hash"], _hash_token(secret))


# ── oauth_codes (authorization codes) ──────────────────────────────────────


@dataclass(frozen=True)
class AuthorizationCode:
    code: str
    client_id: str
    redirect_uri: str
    scope: str | None
    code_challenge: str
    code_challenge_method: str
    user_sub: str
    user_email: str | None
    expires_at: int  # unix ts


def save_authorization_code(
    conn: sqlite3.Connection,
    *,
    client_id: str,
    redirect_uri: str,
    scope: str | None,
    code_challenge: str,
    code_challenge_method: str,
    user_sub: str,
    user_email: str | None,
    ttl_seconds: int = 600,
) -> AuthorizationCode:
    # The plaintext `code` is what we hand back to the client; the DB
    # stores ONLY its sha256 hash (same pattern as refresh tokens). A
    # backup/snapshot leak then doesn't expose live exchangeable codes
    # during their 10-min TTL window. Closes Sec audit finding I8.
    code = f"nfac_{secrets.token_urlsafe(32)}"
    code_hash = _hash_token(code)
    expires_at = int(time.time()) + ttl_seconds

    with conn:
        conn.execute(
            """
            INSERT INTO oauth_codes (
                code, client_id, redirect_uri, scope,
                code_challenge, code_challenge_method,
                user_sub, user_email, expires_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                code_hash,
                client_id,
                redirect_uri,
                scope,
                code_challenge,
                code_challenge_method,
                user_sub,
                user_email,
                _iso(expires_at),
                _now_iso(),
            ),
        )

    return AuthorizationCode(
        code=code,  # plaintext returned to caller (one-time)
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        user_sub=user_sub,
        user_email=user_email,
        expires_at=expires_at,
    )


def consume_authorization_code(conn: sqlite3.Connection, code: str) -> AuthorizationCode | None:
    """Atomically get + delete an authorization code (one-time use).

    Looks up by hash (the DB only ever holds the hash); the caller passes
    in the plaintext code from the client's authorization_code grant.

    Atomic via ``DELETE ... RETURNING``: SQLite serializes the writer side,
    so if two concurrent /oauth/token requests race the same code, only
    ONE sees a returned row — the other gets None and gets a normal
    "invalid_grant" response. The previous SELECT-then-DELETE pattern
    was non-atomic: both readers passed the existence check on the WAL
    snapshot, both DELETEs returned rowcount semantics that didn't
    block the duplicate (audit finding — race condition on RFC 6749
    one-shot codes).
    """
    code_hash = _hash_token(code)
    with conn:
        row = conn.execute(
            "DELETE FROM oauth_codes WHERE code = ? RETURNING *",
            (code_hash,),
        ).fetchone()
    if row is None:
        return None

    expires_at_dt = datetime.fromisoformat(row["expires_at"])
    # Enforce the 10-minute TTL at consume, not just via the periodic
    # Pruner. The DELETE above already removed the row (single-use), so an
    # expired code can't be retried — it simply fails as invalid_grant.
    # Without this, a code leaked/snapshot-recovered before the Pruner
    # swept it (up to ~6h later) stayed exchangeable. (Security M1.)
    if expires_at_dt < datetime.now(UTC):
        return None
    return AuthorizationCode(
        code=code,  # return the plaintext we were given (callers expect it)
        client_id=row["client_id"],
        redirect_uri=row["redirect_uri"],
        scope=row["scope"],
        code_challenge=row["code_challenge"],
        code_challenge_method=row["code_challenge_method"],
        user_sub=row["user_sub"],
        user_email=row["user_email"],
        expires_at=int(expires_at_dt.timestamp()),
    )


# ── oauth_login_state (identity-backend dance) ─────────────────────────────


@dataclass(frozen=True)
class LoginState:
    state: str
    client_id: str
    redirect_uri: str
    scope: str | None
    code_challenge: str
    code_challenge_method: str
    client_state: str | None
    expires_at: int


def save_login_state(
    conn: sqlite3.Connection,
    *,
    client_id: str,
    redirect_uri: str,
    scope: str | None,
    code_challenge: str,
    code_challenge_method: str,
    client_state: str | None,
    ttl_seconds: int = 600,
) -> LoginState:
    # Same pattern as authorization codes (Sec I8): plaintext to caller,
    # sha256 to DB. The state is the CSRF anchor for the OAuth dance —
    # a snapshot/backup leak during its 10-min TTL would otherwise let
    # an attacker reuse it to swap in their own authorization_code.
    state = secrets.token_urlsafe(32)
    state_hash = _hash_token(state)
    expires_at = int(time.time()) + ttl_seconds

    with conn:
        conn.execute(
            """
            INSERT INTO oauth_login_state (
                state, client_id, redirect_uri, scope,
                code_challenge, code_challenge_method,
                client_state, expires_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state_hash,
                client_id,
                redirect_uri,
                scope,
                code_challenge,
                code_challenge_method,
                client_state,
                _iso(expires_at),
                _now_iso(),
            ),
        )

    return LoginState(
        state=state,  # plaintext returned to caller for the OAuth redirect
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        client_state=client_state,
        expires_at=expires_at,
    )


def consume_login_state(conn: sqlite3.Connection, state: str) -> LoginState | None:
    """Atomically get + delete a login-state row.

    Same DELETE ... RETURNING pattern as ``consume_authorization_code``;
    two concurrent /oauth/identity/github/callback requests carrying the
    same state value will see exactly one success and one None.
    """
    state_hash = _hash_token(state)
    with conn:
        row = conn.execute(
            "DELETE FROM oauth_login_state WHERE state = ? RETURNING *",
            (state_hash,),
        ).fetchone()
    if row is None:
        return None

    expires_at_dt = datetime.fromisoformat(row["expires_at"])
    # Enforce the TTL at consume — see consume_authorization_code. An
    # expired login-state (CSRF anchor) must not be replayable. (Security M1.)
    if expires_at_dt < datetime.now(UTC):
        return None
    return LoginState(
        state=state,  # return the plaintext we were given
        client_id=row["client_id"],
        redirect_uri=row["redirect_uri"],
        scope=row["scope"],
        code_challenge=row["code_challenge"],
        code_challenge_method=row["code_challenge_method"],
        client_state=row["client_state"],
        expires_at=int(expires_at_dt.timestamp()),
    )


# ── oauth_refresh_tokens ───────────────────────────────────────────────────


@dataclass(frozen=True)
class RefreshTokenRecord:
    token_hash: str
    client_id: str
    user_sub: str
    scope: str | None
    expires_at: int


def issue_refresh_token(
    conn: sqlite3.Connection,
    *,
    client_id: str,
    user_sub: str,
    scope: str | None,
    ttl_seconds: int,
) -> str:
    """Mint a refresh token, store its hash, return the plaintext to the caller."""
    token = f"nfrt_{secrets.token_urlsafe(32)}"
    expires_at = int(time.time()) + ttl_seconds

    with conn:
        conn.execute(
            """
            INSERT INTO oauth_refresh_tokens (
                token_hash, client_id, user_sub, scope,
                expires_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _hash_token(token),
                client_id,
                user_sub,
                scope,
                _iso(expires_at),
                _now_iso(),
            ),
        )
    return token


def lookup_refresh_token(conn: sqlite3.Connection, token: str) -> RefreshTokenRecord | None:
    """Verify a refresh token by hash, return its metadata if valid + unrevoked."""
    row = conn.execute(
        """
        SELECT * FROM oauth_refresh_tokens
        WHERE token_hash = ? AND revoked_at IS NULL
        """,
        (_hash_token(token),),
    ).fetchone()
    if row is None:
        return None
    expires_at_dt = datetime.fromisoformat(row["expires_at"])
    if expires_at_dt.timestamp() < time.time():
        return None
    return RefreshTokenRecord(
        token_hash=row["token_hash"],
        client_id=row["client_id"],
        user_sub=row["user_sub"],
        scope=row["scope"],
        expires_at=int(expires_at_dt.timestamp()),
    )


def revoke_refresh_token(conn: sqlite3.Connection, token: str) -> bool:
    """Mark a refresh token revoked. Idempotent; returns True if a row was changed."""
    with conn:
        cursor = conn.execute(
            """
            UPDATE oauth_refresh_tokens
            SET revoked_at = ?
            WHERE token_hash = ? AND revoked_at IS NULL
            """,
            (_now_iso(), _hash_token(token)),
        )
    return (cursor.rowcount or 0) > 0
