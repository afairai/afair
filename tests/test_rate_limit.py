"""Unit tests for TokenBucketRateLimiter — pure logic, no HTTP plumbing."""

from __future__ import annotations

import pytest

from afair.mcp.rate_limit import TokenBucketRateLimiter


def test_initial_burst_capacity_allows_2x_minute_rate() -> None:
    """A fresh bucket holds capacity = rpm * multiplier tokens, so 240
    immediate requests for a 120-rpm / 2.0-burst limiter all pass."""
    rl = TokenBucketRateLimiter(requests_per_minute=120, burst_multiplier=2.0)
    for i in range(240):
        allowed, _ = rl.check("alice", now=0.0)
        assert allowed, f"expected first {240} to pass; failed at i={i}"
    # 241st in the same instant is denied.
    allowed, retry_after = rl.check("alice", now=0.0)
    assert not allowed
    assert retry_after > 0


def test_tokens_refill_at_configured_rate() -> None:
    """rpm=60 → 1 token/sec. After draining the bucket, waiting 1 second
    should free exactly 1 token."""
    rl = TokenBucketRateLimiter(requests_per_minute=60, burst_multiplier=1.0)
    # Drain (capacity=60).
    for _ in range(60):
        assert rl.check("bob", now=0.0)[0]
    # Immediately denied.
    assert not rl.check("bob", now=0.0)[0]
    # 1 second later → 1 token available.
    allowed, _ = rl.check("bob", now=1.0)
    assert allowed
    # The very next call (same instant) → denied again.
    assert not rl.check("bob", now=1.0)[0]


def test_buckets_are_per_identity() -> None:
    """A's bucket emptying doesn't affect B's."""
    rl = TokenBucketRateLimiter(requests_per_minute=10, burst_multiplier=1.0)
    for _ in range(10):
        assert rl.check("alice", now=0.0)[0]
    assert not rl.check("alice", now=0.0)[0]
    # Bob still has a full bucket.
    assert rl.check("bob", now=0.0)[0]


def test_retry_after_reflects_token_deficit() -> None:
    """When denied, retry_after = (1 - tokens_remaining) / rate. At 60rpm
    (1 tok/sec), if we just consumed the last token, retry_after ≈ 1s."""
    rl = TokenBucketRateLimiter(requests_per_minute=60, burst_multiplier=1.0)
    for _ in range(60):
        rl.check("c", now=0.0)
    _, retry_after = rl.check("c", now=0.0)
    # Bucket sits at 0.0; need 1.0; rate = 1/sec → retry_after ≈ 1.0
    assert 0.9 < retry_after < 1.2


def test_lru_eviction_bounds_memory() -> None:
    """With a small max_identities cap, the oldest-used bucket is
    evicted when a new identity arrives."""
    rl = TokenBucketRateLimiter(requests_per_minute=10, burst_multiplier=1.0, max_identities=3)
    rl.check("a", now=0.0)
    rl.check("b", now=0.0)
    rl.check("c", now=0.0)
    assert rl.size() == 3
    rl.check("d", now=0.0)
    assert rl.size() == 3  # one of a/b/c was evicted


def test_validation_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError, match="requests_per_minute"):
        TokenBucketRateLimiter(requests_per_minute=0)
    with pytest.raises(ValueError, match="burst_multiplier"):
        TokenBucketRateLimiter(requests_per_minute=10, burst_multiplier=0.5)


def test_long_idle_caps_at_capacity_not_unbounded() -> None:
    """A bucket sitting idle for hours doesn't accumulate infinite tokens
    — it caps at capacity. Prevents 'idle attack window' where saving up
    tokens lets a single huge burst through later."""
    rl = TokenBucketRateLimiter(requests_per_minute=10, burst_multiplier=1.0)
    rl.check("e", now=0.0)  # creates bucket, capacity=10
    # 1 hour later — should still cap at 10, not 600.
    for _ in range(10):
        assert rl.check("e", now=3600.0)[0]
    assert not rl.check("e", now=3600.0)[0]


def test_reset_clears_all_buckets() -> None:
    rl = TokenBucketRateLimiter(requests_per_minute=10, burst_multiplier=1.0)
    for _ in range(10):
        rl.check("x", now=0.0)
    assert rl.size() == 1
    rl.reset()
    assert rl.size() == 0
    # Fresh start — full burst available again.
    assert rl.check("x", now=0.0)[0]


def test_query_cache_ttl_expires_stale_entries() -> None:
    """Embedding-side: an entry older than ttl is treated as a miss
    even when the LRU bound hasn't been reached. Prevents stale
    embeddings from sitting in memory forever."""
    import time as time_module
    from unittest.mock import patch

    from afair.agents.embedding import _QueryEmbeddingCache

    cache = _QueryEmbeddingCache(maxsize=100, ttl_seconds=60)

    calls = {"n": 0}

    def fake_embed(**_: object) -> list[float]:
        calls["n"] += 1
        return [0.1] * 4

    with patch("afair.agents.embedding.embed_text", fake_embed):
        # First call — miss.
        cache.get_or_compute(model="m", text="q", api_key=None)
        assert calls["n"] == 1
        # Second call within TTL — hit.
        cache.get_or_compute(model="m", text="q", api_key=None)
        assert calls["n"] == 1
        # Jump past TTL — next call is a miss again.
        with patch.object(time_module, "monotonic", return_value=time_module.monotonic() + 120):
            cache.get_or_compute(model="m", text="q", api_key=None)
        assert calls["n"] == 2
    stats = cache.stats()
    assert stats["expired"] >= 1
