#!/usr/bin/env python3
"""End-to-end latency bench against an MCP server.

Run examples:
    # Local dev server (http://localhost:8765, the default)
    uv run scripts/bench.py

    # Any deployment
    AFAIR_URL=https://memory.example.com/mcp \\
    AFAIR_AUTH_TOKEN=... uv run scripts/bench.py

    # ...or with flags
    uv run scripts/bench.py --url https://memory.example.com/mcp --token …

What it reports:
    - initialize:   one-shot TLS + MCP handshake cost
    - cold:         first call per query string (cache miss, full embedding)
    - warm:         second/third call per query string (cache hit, no API)
    - shallow:      FTS5 only, no semantic search
    - per category: min / p50 / p95 / max / mean
    - network floor:approx network RTT (initialize - server-side budget)

Compare across runs — same bench should produce stable numbers ±20ms.
Outliers > 2s usually mean a Fly cold-start; just re-run.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://localhost:8765/mcp"

DEFAULT_QUERIES = [
    "constitutional invariants memory ownership",
    "what we shipped today across vendors",
    "sajinth roadmap projects",
    "fts regression bug fix",
    "phase 1 sprint a deliverables",
]


def _load_token_from_env_local() -> str | None:
    """Best-effort token discovery from .env.local in the repo root.

    Looks for ``AFAIR_AUTH_TOKEN=<value>`` lines, trims quotes and
    whitespace. Returns None if not found — callers can pass via env var.
    """
    candidates = [Path(".env.local"), Path(".env"), Path("../.env.local")]
    for path in candidates:
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            if line.startswith("AFAIR_AUTH_TOKEN="):
                value = line.split("=", 1)[1].strip().strip("'\"")
                if value:
                    return value
    return None


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    sorted_v = sorted(values)
    k = (len(sorted_v) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_v) - 1)
    return sorted_v[f] + (sorted_v[c] - sorted_v[f]) * (k - f)


class MCPClient:
    """Minimal MCP-over-Streamable-HTTP client for benchmarking.

    Reuses the underlying urllib connection across requests to mirror
    real-client behaviour (Claude Code, Codex, Claude.ai all keep a
    persistent transport per session).
    """

    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token
        self.session_id: str | None = None

    def _post(self, payload: dict) -> tuple[str, float]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Accept-Encoding": "gzip",
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read().decode("utf-8", errors="replace")
                if self.session_id is None:
                    self.session_id = r.headers.get("mcp-session-id")
            elapsed_ms = (time.perf_counter() - t0) * 1000
            return body, elapsed_ms
        except urllib.error.HTTPError as e:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            return f"HTTP {e.code}: {e.read().decode(errors='replace')}", elapsed_ms

    def initialize(self) -> float:
        _, ms = self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "bench", "version": "1"},
                },
            }
        )
        # The "initialized" notification doesn't return a body but must be sent
        # before subsequent tool calls.
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        return ms

    def recall(self, query: str, depth: str = "normal", limit: int = 5) -> float:
        _, ms = self._post(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "recall",
                    "arguments": {"query": query, "depth": depth, "limit": limit},
                },
            }
        )
        return ms


def _bench_category(label: str, samples: list[float]) -> None:
    if not samples:
        print(f"  {label:14s}  (no samples)")
        return
    print(
        f"  {label:14s}  "
        f"min {min(samples):5.0f}ms  "
        f"p50 {_percentile(samples, 50):5.0f}ms  "
        f"p95 {_percentile(samples, 95):5.0f}ms  "
        f"max {max(samples):5.0f}ms  "
        f"mean {statistics.mean(samples):5.0f}ms  "
        f"(n={len(samples)})"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Bench the MCP server latency.")
    parser.add_argument(
        "--url",
        default=os.environ.get("AFAIR_URL"),
        help=f"MCP endpoint URL (default: {DEFAULT_URL}, or $AFAIR_URL)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("AFAIR_AUTH_TOKEN"),
        help="Bearer token (default: auto-discover from .env.local)",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=3,
        help="Samples per query per category (default: 3)",
    )
    parser.add_argument(
        "--queries",
        type=int,
        default=len(DEFAULT_QUERIES),
        help=f"How many of the default queries to use (max {len(DEFAULT_QUERIES)})",
    )
    args = parser.parse_args()

    url = args.url or DEFAULT_URL
    token = args.token or _load_token_from_env_local()
    if not token:
        print("ERROR: no bearer token provided. Set AFAIR_AUTH_TOKEN or --token.")
        return 1

    queries = DEFAULT_QUERIES[: args.queries]
    print(f"target:   {url}")
    print(f"queries:  {len(queries)}  ({args.samples} samples per category)")
    print()

    client = MCPClient(url=url, token=token)
    init_ms = client.initialize()
    print(f"initialize ({len(token)}-char token, fresh session): {init_ms:.0f}ms")
    print()

    cold_ms: list[float] = []
    warm_ms: list[float] = []
    shallow_ms: list[float] = []

    # Cold: each query first time (cache miss after embedding call)
    print("running cold pass (cache miss) ...")
    for q in queries:
        ms = client.recall(q, depth="normal")
        cold_ms.append(ms)

    # Warm: same queries repeated for warm cache hits
    print("running warm pass (cache hits) ...")
    for _ in range(args.samples):
        for q in queries:
            ms = client.recall(q, depth="normal")
            warm_ms.append(ms)

    # Shallow: FTS-only, no semantic / no embedding call
    print("running shallow pass (FTS only) ...")
    for _ in range(args.samples):
        for q in queries:
            ms = client.recall(q, depth="shallow")
            shallow_ms.append(ms)

    print()
    print("results:")
    _bench_category("initialize", [init_ms])
    _bench_category("recall cold", cold_ms)
    _bench_category("recall warm", warm_ms)
    _bench_category("recall shallow", shallow_ms)
    print()

    # Quick read of the breakdown.
    network_floor = statistics.median(shallow_ms) if shallow_ms else None
    if network_floor is not None:
        emb_cost_estimate = statistics.median(cold_ms) - network_floor if cold_ms else 0
        print(f"network floor (≈shallow median):        {network_floor:5.0f}ms")
        print(f"embedding/vec cost (cold - shallow):    {emb_cost_estimate:+.0f}ms")
        print(
            f"cache speedup (cold→warm):              "
            f"{(statistics.median(cold_ms) - statistics.median(warm_ms)):+.0f}ms saved"
            if warm_ms
            else ""
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
