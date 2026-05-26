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
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp


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


def _identity_from_request(request: Request) -> str | None:
    """Derive a stable per-identity key from the request's auth header.

    We hash the raw token rather than store it as the dict key — keeps
    secrets out of memory dumps, debugger inspections, and structlog
    output. SHA-256 is overkill for a 4096-bound dict but the few extra
    microseconds are invisible.
    """
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    token = auth[7:].strip()
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Starlette middleware applying per-identity rate-limiting to /mcp
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
        super().__init__(app)
        self._limiter = limiter
        self._exempt = frozenset(exempt_paths)
        self._exempt_prefixes = tuple(exempt_prefixes)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        if path in self._exempt:
            return await call_next(request)
        if any(path.startswith(p) for p in self._exempt_prefixes):
            return await call_next(request)

        identity = _identity_from_request(request)
        if identity is None:
            # No bearer — auth middleware will reject. Don't burn a bucket.
            return await call_next(request)

        allowed, retry_after = self._limiter.check(identity)
        if allowed:
            return await call_next(request)

        # 429 Too Many Requests — RFC 6585.
        retry_after_seconds = max(1, int(retry_after) + 1)
        return JSONResponse(
            {
                "error": "rate_limited",
                "detail": "too many requests for this identity",
                "retry_after_seconds": retry_after_seconds,
            },
            status_code=429,
            headers={
                "Retry-After": str(retry_after_seconds),
                "X-RateLimit-Limit": str(int(self._limiter.rate_per_sec * 60)),
            },
        )
