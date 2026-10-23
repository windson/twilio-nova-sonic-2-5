"""Offline event-loop responsiveness probe; this is not a voice latency benchmark.

Run: .venv/bin/python -m scripts.benchmark_async_io
The same fake 120 ms blocking store is invoked directly and through the
application's async persistence path. No cloud/API call is made.
"""
from __future__ import annotations

import asyncio
import json
import logging
import platform
import statistics
import time

from app import main as app_main
from app.metrics import CallMetrics
from app.store import InMemoryStore

SAMPLES = 5
FAKE_IO_SECONDS = 0.12
HEARTBEAT_SECONDS = 0.005


class DelayedStore(InMemoryStore):
    def save_call_analytics(self, record):
        time.sleep(FAKE_IO_SECONDS)
        return super().save_call_analytics(record)


async def sample(offloaded: bool) -> dict[str, float]:
    log = logging.getLogger("offline_benchmark")
    log.handlers = [logging.NullHandler()]
    log.propagate = False
    metrics = CallMetrics(call_sid="CA_offline_benchmark", logger_=log)
    metrics.on_session_start()
    metrics.on_session_end()
    gaps = []
    done = False

    async def heartbeat():
        previous = time.perf_counter()
        while not done:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            now = time.perf_counter()
            gaps.append((now - previous) * 1000)
            previous = now

    task = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.015)
    started = time.perf_counter()
    store = DelayedStore()
    if offloaded:
        await app_main._persist_analytics(metrics, store, "MZ_offline")
    else:
        store.save_call_analytics(metrics.to_call_analytics())
    io_elapsed_ms = (time.perf_counter() - started) * 1000
    await asyncio.sleep(0.015)
    done = True
    await task
    return {"io_elapsed_ms": round(io_elapsed_ms, 3), "maximum_heartbeat_gap_ms": round(max(gaps), 3)}


async def main():
    # Keep the probe entirely offline: the actual persistence code calls this
    # scheduling seam only after its fake write completes.
    original = app_main._launch_postcall
    app_main._launch_postcall = lambda *_: None
    try:
        results = {}
        for mode, offloaded in [("direct_blocking_baseline", False), ("async_persistence", True)]:
            values = [await sample(offloaded) for _ in range(SAMPLES)]
            results[mode] = {"samples": values, "median_maximum_heartbeat_gap_ms": round(statistics.median(
                value["maximum_heartbeat_gap_ms"] for value in values), 3)}
        print(json.dumps({"scope": "offline fake SDK I/O; not live call TTFT/TTFA",
                          "python": platform.python_version(), "samples_per_mode": SAMPLES,
                          "fake_io_ms": FAKE_IO_SECONDS * 1000, "heartbeat_interval_ms": HEARTBEAT_SECONDS * 1000,
                          "results": results}, indent=2))
    finally:
        app_main._launch_postcall = original


if __name__ == "__main__":
    asyncio.run(main())
