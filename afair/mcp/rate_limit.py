"""Per-identity rate limiting — token-bucket, in-memory.

Why this exists
---------------
The server accepts a static bearer + JWTs over the public internet. If a
token leaks, an attacker can hammer ``recall``/``remember``/``invalidate``
endlessly — each call may trigger an Anthropic LLM call (~$0.001) plus a
local FastEmbed inference. At 100/sec sustained, a leaked token costs
real money fast.

A per-identity rate limit caps the blast radius. In-memory token bucket
with sane defaults: each authenticated identity gets N requests/min with
burst capacity 2xN. Exempt paths (landing, /health, OAuth dance) skip
the check.

The middleware identifies callers by ``Authorization: Bearer …``
header. Same identity (same token / JWT) = same bucket. Different
tokens = different buckets. Unauthenticated requests (which only the
exempt paths permit) don't consume buckets.

Single-tenant per I8 means we don't need cluster-wide synchronization;
in-memory is sufficient. Phase 8 (multi-machine managed hosting) keeps
the same shape — each machine has its own bucket because each user has
their own machine.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .auth import SCOPE_IDENTITY_KEY

if TYPE_CHECKING:
    from collections.abc import Iterable

    from starlette.types import ASGIApp, Receive, Scope, Send


DEFAULT_REQUESTS_PER_MINUTE = 120
"""Default per-identity cap. 120/min = 2/sec sustained, with burst to
240 in any 60s window via the bucket carry. Generous for normal use
(an interactive Claude.ai session does maybe 5/min); aggressive enough
to make a token-leak attack land slowly."""

DEFAULT_BURST_MULTIPLIER = 2.0
"""Bucket capacity = rate-per-minute x multiplier. Allows short bursts
above sustained rate without false-rejecting."""

_MAX_BUCKETS = 4096
"""LRU bound on the bucket dict. Even with rotating identities this
keeps memory bounded. Per-token tracking, single-tenant — realistic
upper bound is dozens, not thousands."""


@dataclass
class _Bucket:
    """A token bucket. Tokens refill linearly; ``available`` is computed
    on-demand rather than via a timer to avoid background work."""

    tokens: float
    last_refill: float


class TokenBucketRateLimiter:
    """Thread-safe in-memory token-bucket rate limiter.

    Independent of HTTP — could be reused for queue ingestion, webhook
    receipt, or any other per-identity throttle. Tests can construct
    directly without spinning up Starlette.
    """

    def __init__(
        self,
        *,
        requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE,
        burst_multiplier: float = DEFAULT_BURST_MULTIPLIER,
        max_identities: int = _MAX_BUCKETS,
    ) -> None:
        if requests_per_minute < 1:
            msg = f"requests_per_minute must be >= 1; got {requests_per_minute}"
            raise ValueError(msg)
        if burst_multiplier < 1.0:
            msg = f"burst_multiplier must be >= 1.0; got {burst_multiplier}"
            raise ValueError(msg)
        self.rate_per_sec = requests_per_minute / 60.0
        self.capacity = float(requests_per_minute) * burst_multiplier
        self._max_identities = max_identities
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._lock = threading.Lock()

    def check(self, identity: str, *, now: float | None = None) -> tuple[bool, float]:
        """Return ``(allowed, retry_after_seconds)``.

        ``allowed=True`` means consume a token and proceed. ``allowed=False``
        means deny; ``retry_after_seconds`` is how long until at least one
        token will be available.
        """
        now = now if now is not None else time.monotonic()
        with self._lock:
            bucket = self._buckets.get(identity)
            if bucket is None:
                bucket = _Bucket(tokens=self.capacity, last_refill=now)
                self._buckets[identity] = bucket
                self._evict_if_full()
            else:
                # Refill since last check.
                elapsed = max(0.0, now - bucket.last_refill)
                bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.rate_per_sec)
                bucket.last_refill = now
                # Mark as recently-used in the LRU.
                self._buckets.move_to_end(identity)

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True, 0.0
            # Compute time until next token (we need 1.0 tokens; we have N<1).
            deficit = 1.0 - bucket.tokens
            retry_after = deficit / self.rate_per_sec if self.rate_per_sec > 0 else 1.0
            return False, retry_after

    def _evict_if_full(self) -> None:
        """LRU eviction — drop oldest entry when over capacity."""
        while len(self._buckets) > self._max_identities:
            self._buckets.popitem(last=False)

    def reset(self) -> None:
        """For tests; not used in production."""
        with self._lock:
            self._buckets.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._buckets)


_AUTHORIZATION = b"authorization"


def _scope_identity(scope: Scope) -> str | None:
    """Derive a stable per-identity key for the rate-limit bucket.

    Precedence:
      1. ``scope[SCOPE_IDENTITY_KEY]`` set by the auth middleware —
         uses the verified JWT subject (``jwt:<sub>``) or a constant
         label for the static bearer. Same identity stays in one bucket
         across refreshes / rotations (Sec audit I2).
      2. Fallback: hash the raw bearer bytes. Reached only when this
         middleware runs without the auth middleware in front of it
         (tests, or future endpoints that wire it alone). Keeps the
         old behavior for those callers.
    """
    state_identity = scope.get(SCOPE_IDENTITY_KEY)
    if isinstance(state_identity, str) and state_identity:
        return state_identity

    auth = ""
    for k, v in scope.get("headers", []):
        if k.lower() == _AUTHORIZATION:
            try:
                auth = v.decode("latin-1")
            except UnicodeDecodeError:
                return None
            break
    if not auth.lower().startswith("bearer "):
        return None
    token = auth[7:].strip()
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]


class RateLimitMiddleware:
    """ASGI middleware applying per-identity rate-limiting to /mcp
    and other authenticated paths. Exempt paths bypass the check entirely.

    Order matters: this should sit BELOW the bearer/JWT auth middleware
    so we never rate-limit unauthenticated requests (they 401 first).
    Putting it ABOVE auth would mean an attacker could exhaust the bucket
    map with random tokens.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        limiter: TokenBucketRateLimiter,
        exempt_paths: Iterable[str] = (),
        exempt_prefixes: Iterable[str] = (),
    ) -> None:
        self.app = app
        self._limiter = limiter
        self._exempt = frozenset(exempt_paths)
        self._exempt_prefixes = tuple(exempt_prefixes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in self._exempt or any(path.startswith(p) for p in self._exempt_prefixes):
            await self.app(scope, receive, send)
            return

        identity = _scope_identity(scope)
        if identity is None:
            # No bearer — auth middleware will reject. Don't burn a bucket.
            await self.app(scope, receive, send)
            return

        allowed, retry_after = self._limiter.check(identity)
        if allowed:
            await self.app(scope, receive, send)
            return

        # 429 Too Many Requests — RFC 6585.
        retry_after_seconds = max(1, int(retry_after) + 1)
        body = json.dumps(
            {
                "error": "rate_limited",
                "detail": "too many requests for this identity",
                "retry_after_seconds": retry_after_seconds,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"retry-after", str(retry_after_seconds).encode("ascii")),
                    (
                        b"x-ratelimit-limit",
                        str(int(self._limiter.rate_per_sec * 60)).encode("ascii"),
                    ),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})


_FLY_CLIENT_IP = b"fly-client-ip"
_X_FORWARDED_FOR = b"x-forwarded-for"


def client_ip_from_scope(scope: Scope, *, environment: str) -> str:
    """Best-effort caller IP for per-IP rate limiting, environment-aware.

    Behind the Fly proxy (``environment == "fly"``) the platform sets
    ``Fly-Client-IP`` and overwrites any client-supplied value, so we trust
    ONLY that header and deliberately ignore ``X-Forwarded-For`` — otherwise
    an attacker could rotate a spoofed XFF to evade the per-IP bucket. Off
    Fly we fall back to the first XFF hop, then the socket. (Security: XFF
    spoofing on the per-IP limiter.)
    """
    headers: list[tuple[bytes, bytes]] = scope.get("headers", [])

    def _get(name: bytes) -> str | None:
        for k, v in headers:
            if k.lower() == name:
                try:
                    return v.decode("latin-1").strip()
                except UnicodeDecodeError:
                    return None
        return None

    fly = _get(_FLY_CLIENT_IP)
    if fly:
        return fly
    if environment == "fly":
        # Behind Fly the proxy always sets Fly-Client-IP. Its absence means
        # the request did not transit the proxy — do not trust client XFF.
        return "unknown"
    xff = _get(_X_FORWARDED_FOR)
    if xff:
        first = xff.split(",", 1)[0].strip()
        if first:
            return first
    client = scope.get("client")
    if client and client[0]:
        return str(client[0])
    return "unknown"


class InternalPathRateLimitMiddleware:
    """Per-IP rate limit for the ``/internal/*`` routes.

    Those routes (signup, export, tokens) carry their own scoped bearer and
    are therefore exempt from ``BearerOrJwtMiddleware`` AND the main
    ``RateLimitMiddleware`` (which buckets by authenticated identity). That
    left them with no throttle at all — a leaked scoped bearer could hammer
    signup (vault spam), export (repeated full-vault streaming), or tokens
    (mint/list) at full speed. This middleware closes that gap by bucketing
    those specific paths per client IP. (Security L5.)
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        limiter: TokenBucketRateLimiter,
        environment: str,
        protected_prefixes: Iterable[str] = (),
    ) -> None:
        self.app = app
        self._limiter = limiter
        self._environment = environment
        self._prefixes = tuple(protected_prefixes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if not any(path.startswith(p) for p in self._prefixes):
            await self.app(scope, receive, send)
            return

        ip = client_ip_from_scope(scope, environment=self._environment)
        allowed, retry_after = self._limiter.check(f"internal:{ip}")
        if allowed:
            await self.app(scope, receive, send)
            return

        retry_after_seconds = max(1, int(retry_after) + 1)
        body = json.dumps(
            {
                "error": "rate_limited",
                "detail": "too many requests from this IP",
                "retry_after_seconds": retry_after_seconds,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"retry-after", str(retry_after_seconds).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})
