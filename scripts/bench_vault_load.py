"""Measure local mixed workload in a disposable, exclusively synthetic vault."""

from __future__ import annotations

import argparse
import json
import resource
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import structlog
from ulid import ULID

from afair.agents.embedding import EmbeddingError
from afair.agents.extraction_recovery import discover_missing_extractions
from afair.mcp import handlers
from afair.mcp.context import ServerContext, clear_context, set_context
from afair.mcp.export_route import _iter_export
from afair.mcp.schemas import ObserveEvent
from afair.substrate import open_db, write_event
from scripts.bench import _percentile


def measure(root: Path, seeds: int, calls: int) -> dict:
    # The CLI supplies a new temporary directory; never accept a caller vault.
    if any(root.iterdir()):
        raise ValueError("benchmark requires empty directory")
    db = open_db(root)
    historical = datetime.now(UTC) - timedelta(hours=1)
    prefix = int(historical.timestamp() * 1000).to_bytes(6, "big")
    ids = (str(ULID.from_bytes(prefix + i.to_bytes(10, "big"))) for i in range(seeds))
    with patch("afair.substrate.events._new_id", side_effect=ids):
        for idx in range(seeds):
            text = f"Notebook {idx % 100} stores synthetic sample {idx}."
            write_event(
                db,
                created_at=historical.isoformat(),
                origin="bench",
                kind="remember",
                payload={"content_type": "text", "text": text},
                searchable_body=text,
            )
    config = ServerContext(
        vault_dir=root, inline_text_max_bytes=65536, semantic_recall_enabled=True
    )
    set_context(config)
    stop = threading.Event()
    peak = {"disk_bytes": 0, "wal_bytes": 0, "pending_age_seconds": None}

    def sample():
        monitor = open_db(root)
        try:
            while True:
                files = {p.name: p.stat().st_size for p in root.iterdir() if p.is_file()}
                peak["disk_bytes"] = max(peak["disk_bytes"], sum(files.values()))
                peak["wal_bytes"] = max(
                    peak["wal_bytes"], sum(n for k, n in files.items() if k.endswith("-wal"))
                )
                age = monitor.execute(
                    "SELECT MAX(unixepoch('now')-unixepoch(e.created_at)) FROM extraction_jobs j JOIN events e ON e.id=j.event_id WHERE j.terminal=0"
                ).fetchone()[0]
                if age is not None:
                    peak["pending_age_seconds"] = max(0, peak["pending_age_seconds"] or 0, age)
                if stop.wait(0.05):
                    return
        finally:
            monitor.close()

    def call(index):
        start = time.monotonic()
        if index % 2:
            result = handlers.recall(query=f"Notebook {index % 100}", depth="normal", limit=5)
            return ("recall", (time.monotonic() - start) * 1000, result.depth_used)
        handlers.observe(ObserveEvent(action="sampled", subject=f"synthetic-{index}"))
        return ("write", (time.monotonic() - start) * 1000, None)

    # Deterministic provider outage. Intake persists data, real discovery
    # can queue it, and recall exercises the real keyword fallback.
    def recovery():
        conn = open_db(root)
        try:
            discover_missing_extractions(conn)
            return conn.execute("SELECT COUNT(*) FROM extraction_jobs").fetchone()[0]
        finally:
            conn.close()

    try:
        with (
            patch.object(handlers, "schedule_extraction"),
            patch.object(handlers, "embed_query", side_effect=EmbeddingError("synthetic outage")),
            ThreadPoolExecutor(max_workers=8) as pool,
        ):
            sampler = pool.submit(sample)
            try:
                export = pool.submit(lambda: sum(1 for _ in _iter_export(root, include_blobs=True)))
                repair = pool.submit(recovery)
                outcomes = list(pool.map(call, range(calls)))
                exported, discovered = export.result(), repair.result()
            finally:
                stop.set()
                sampler.result()
        stats = {}
        for kind in ("recall", "write"):
            values = [lat for operation, lat, _ in outcomes if operation == kind]
            stats[kind] = {
                "n": len(values),
                "p50_ms": round(_percentile(values, 50), 2),
                "p95_ms": round(_percentile(values, 95), 2),
            }
        reads = [depth for operation, _, depth in outcomes if operation == "recall"]
        return {
            "seeds": seeds,
            "provider": "simulated outage",
            "concurrency": 8,
            "latency": stats,
            "recall_fallback_rate": sum(d == "shallow" for d in reads) / len(reads),
            "export_records": exported,
            "recovery_queued": discovered,
            "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            * (1 if sys.platform == "darwin" else 1024),
            **peak,
        }
    finally:
        clear_context()
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", type=int, default=20000)
    parser.add_argument("--calls", type=int, default=200)
    args = parser.parse_args()
    if args.seeds < 1 or args.calls < 2:
        parser.error("requires a positive seed count and at least two calls")
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(50))
    with tempfile.TemporaryDirectory(prefix="afair-load-") as tmp:
        print(json.dumps(measure(Path(tmp), args.seeds, args.calls), indent=2))
