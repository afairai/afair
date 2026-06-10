"""Per-IP rate limit on /internal/* + environment-aware client IP.

Security L5 — the /internal/* routes self-auth with a scoped bearer and are
exempt from the identity-bucketed limiter, so this per-IP limiter is their
only throttle. Plus: behind Fly we trust only Fly-Client-IP and ignore a
spoofable X-Forwarded-For.
"""

from __future__ import annotations

import pytest

from afair.mcp.rate_limit import (
    InternalPathRateLimitMiddleware,
    TokenBucketRateLimiter,
    client_ip_from_scope,
)


def _scope(path: str, headers: list[tuple[bytes, bytes]], client_host: str = "10.0.0.1") -> dict:
    return {"type": "http", "path": path, "headers": headers, "client": (client_host, 1234)}


def test_fly_env_trusts_only_fly_client_ip() -> None:
    headers = [(b"x-forwarded-for", b"6.6.6.6"), (b"fly-client-ip", b"1.2.3.4")]
    assert client_ip_from_scope(_scope("/x", headers), environment="fly") == "1.2.3.4"


def test_fly_env_ignores_spoofed_xff_when_no_fly_header() -> None:
    # No Fly-Client-IP behind Fly means the request didn't transit the proxy —
    # the client-supplied XFF must NOT be trusted as the bucket key.
    headers = [(b"x-forwarded-for", b"6.6.6.6")]
    assert client_ip_from_scope(_scope("/x", headers), environment="fly") == "unknown"


def test_non_fly_env_falls_back_to_xff_then_socket() -> None:
    headers = [(b"x-forwarded-for", b"6.6.6.6, 7.7.7.7")]
    assert client_ip_from_scope(_scope("/x", headers), environment="local") == "6.6.6.6"
    assert client_ip_from_scope(_scope("/x", []), environment="local") == "10.0.0.1"


@pytest.mark.asyncio
async def test_internal_paths_are_rate_limited_per_ip() -> None:
    calls = {"n": 0}

    async def app(scope, receive, send):
        calls["n"] += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    limiter = TokenBucketRateLimiter(requests_per_minute=2, burst_multiplier=1.0)
    mw = InternalPathRateLimitMiddleware(
        app,
        limiter=limiter,
        environment="fly",
        protected_prefixes=("/internal/signup",),
    )

    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request"}

    headers = [(b"fly-client-ip", b"1.2.3.4")]
    scope = _scope("/internal/signup", headers)

    # First 2 allowed, 3rd rejected with 429 from the same IP.
    for _ in range(3):
        await mw(scope, receive, send)

    statuses = [m["status"] for m in sent if m["type"] == "http.response.start"]
    assert statuses == [200, 200, 429]
    assert calls["n"] == 2  # third never reached the app


@pytest.mark.asyncio
async def test_non_internal_paths_bypass_the_limiter() -> None:
    calls = {"n": 0}

    async def app(scope, receive, send):
        calls["n"] += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    limiter = TokenBucketRateLimiter(requests_per_minute=1, burst_multiplier=1.0)
    mw = InternalPathRateLimitMiddleware(
        app, limiter=limiter, environment="fly", protected_prefixes=("/internal/signup",)
    )

    async def send(msg):
        pass

    async def receive():
        return {"type": "http.request"}

    headers = [(b"fly-client-ip", b"1.2.3.4")]
    # /mcp is not internal — should never be throttled here regardless of count.
    for _ in range(5):
        await mw(_scope("/mcp", headers), receive, send)
    assert calls["n"] == 5
