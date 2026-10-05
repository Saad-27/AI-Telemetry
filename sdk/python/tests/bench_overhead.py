"""Call-path overhead benchmark (brief §6.8). Exits non-zero if over budget.

Run: uv run python tests/bench_overhead.py

The SDK runs as in production, sending to a local fake ingest from its background thread,
so the numbers include contention with the sender. Times are whole ``track()`` blocks,
not differences from a baseline, so they overstate the true overhead slightly.
"""

from __future__ import annotations

import statistics
import sys
import time
from collections.abc import Callable

from fake_ingest import FakeIngest

import yourpkg

CALL_BUDGET_US = 50.0  # median per call
CHUNK_BUDGET_US = 2.0  # median per streamed chunk
ROUNDS = 200
PER_ROUND = 200


def tracked_call() -> None:
    with yourpkg.track(provider="custom", model="bench", streaming=True) as t:
        t.first_token()
        t.set_usage(input_tokens=1200, output_tokens=300)


def median_us(fn: Callable[[], None], per_round: int = PER_ROUND) -> tuple[float, float]:
    """Median and p99 of per-iteration time over ``ROUNDS`` rounds, in microseconds."""
    times = []
    for _ in range(ROUNDS):
        t0 = time.perf_counter_ns()
        for _ in range(per_round):
            fn()
        times.append((time.perf_counter_ns() - t0) / per_round / 1000)
    times.sort()
    return statistics.median(times), times[int(len(times) * 0.99)]


def main() -> int:
    with FakeIngest() as ingest:
        yourpkg.init(api_key="rm_live_bench", endpoint=ingest.url)
        for _ in range(1000):  # warm up, and start the sender thread
            tracked_call()

        call_med, call_p99 = median_us(tracked_call)

        with yourpkg.track(provider="custom", model="bench", streaming=True) as t:
            t.first_token()
            chunk_med, chunk_p99 = median_us(t.first_token, per_round=10_000)

        yourpkg.shutdown()
        if not ingest.accepted():
            print("FAIL: nothing reached the fake ingest")
            return 1

    py = "{}.{}.{}".format(*sys.version_info[:3])
    print(f"Python {py}: per-call and per-chunk overhead (µs)")
    print(f"  track() call   median {call_med:6.2f}  p99 {call_p99:6.2f}  budget {CALL_BUDGET_US}")
    print(
        f"  streamed chunk median {chunk_med:6.3f}  p99 {chunk_p99:6.3f}  budget {CHUNK_BUDGET_US}"
    )
    if call_med > CALL_BUDGET_US or chunk_med > CHUNK_BUDGET_US:
        print("FAIL: over budget")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
