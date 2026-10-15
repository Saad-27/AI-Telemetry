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

import gaslit

CALL_BUDGET_US = 50.0  # median per call
CHUNK_BUDGET_US = 2.0  # median per streamed chunk
ROUNDS = 200
PER_ROUND = 200


def tracked_call() -> None:
    with gaslit.track(provider="custom", model="bench", streaming=True) as t:
        t.first_token()
        t.set_usage(input_tokens=1200, output_tokens=300)


def median_us(fn: Callable[[], object], per_round: int = PER_ROUND) -> tuple[float, float]:
    """Median and p99 of per-iteration time over ``ROUNDS`` rounds, in microseconds."""
    times = []
    for _ in range(ROUNDS):
        t0 = time.perf_counter_ns()
        for _ in range(per_round):
            fn()
        times.append((time.perf_counter_ns() - t0) / per_round / 1000)
    times.sort()
    return statistics.median(times), times[int(len(times) * 0.99)]


def openai_overhead() -> tuple[float, float, float]:
    """Added time (µs) per patched ``create`` call, and per chat chunk and Responses event.

    Each is the difference between running the wrapper and running the bare stand-in for the
    provider call, so it is the SDK's own cost: host detection, the Call, the usage reading
    and the recording for a call, and one chunk's check for a streamed item.
    """
    from types import SimpleNamespace

    from openai.types.chat import ChatCompletion, ChatCompletionChunk
    from openai.types.responses import ResponseTextDeltaEvent

    from gaslit import _openai
    from gaslit._model import Call

    resource = SimpleNamespace(_client=SimpleNamespace(base_url="https://api.openai.com/v1/"))
    completion = ChatCompletion.model_validate(
        {
            "id": "c", "object": "chat.completion", "created": 1, "model": "gpt-4o",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "x"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )  # fmt: skip

    def provider_call(self: object, **kwargs: object) -> ChatCompletion:
        return completion

    wrapped = _openai._sync_create(provider_call, "chat.completions")
    kwargs = {"model": "gpt-4o", "messages": []}
    bare, _ = median_us(lambda: provider_call(resource, **kwargs))
    measured, _ = median_us(lambda: wrapped(resource, **kwargs))

    def per_item(items: list[object], handler: Callable[..., None]) -> float:
        def consume(measure: bool) -> float:
            call = Call("openai", "direct", "chat.completions", "m", streaming=True)
            it = iter(items)
            if measure:
                it = _openai._SyncMeasure(call, it, handler)
            t0 = time.perf_counter_ns()
            for _ in it:
                pass
            return (time.perf_counter_ns() - t0) / len(items) / 1000

        return statistics.median(consume(True) for _ in range(30)) - statistics.median(
            consume(False) for _ in range(30)
        )

    chunk = ChatCompletionChunk.model_validate(
        {
            "id": "c", "object": "chat.completion.chunk", "created": 1, "model": "gpt-4o",
            "choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": None}],
        }
    )  # fmt: skip
    event = ResponseTextDeltaEvent.construct(type="response.output_text.delta", delta="x")
    return (
        measured - bare,
        per_item([chunk] * 5000, _openai._chat_item),
        per_item([event] * 5000, _openai._event_item),
    )


def main() -> int:
    with FakeIngest() as ingest:
        gaslit.init(api_key="gl_live_bench", endpoint=ingest.url)
        for _ in range(1000):  # warm up, and start the sender thread
            tracked_call()

        call_med, call_p99 = median_us(tracked_call)

        with gaslit.track(provider="custom", model="bench", streaming=True) as t:
            t.first_token()
            chunk_med, chunk_p99 = median_us(t.first_token, per_round=10_000)

        call_openai, chat_chunk, event_chunk = openai_overhead()

        gaslit.shutdown()
        if not ingest.accepted():
            print("FAIL: nothing reached the fake ingest")
            return 1

    py = "{}.{}.{}".format(*sys.version_info[:3])
    print(f"Python {py}: per-call and per-chunk overhead (µs)")
    print(f"  track() call   median {call_med:6.2f}  p99 {call_p99:6.2f}  budget {CALL_BUDGET_US}")
    print(
        f"  streamed chunk median {chunk_med:6.3f}  p99 {chunk_p99:6.3f}  budget {CHUNK_BUDGET_US}"
    )
    print(f"  openai create  added  {call_openai:6.2f}  budget {CALL_BUDGET_US}")
    print(f"  openai chat chunk   {chat_chunk:6.3f}  budget {CHUNK_BUDGET_US}")
    print(f"  openai responses event {event_chunk:6.3f}  budget {CHUNK_BUDGET_US}")
    if (
        call_med > CALL_BUDGET_US
        or chunk_med > CHUNK_BUDGET_US
        or call_openai > CALL_BUDGET_US
        or max(chat_chunk, event_chunk) > CHUNK_BUDGET_US
    ):
        print("FAIL: over budget")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
