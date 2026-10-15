"""Call-path overhead benchmark (brief §6.8). Exits non-zero if over budget.

Run: uv run python tests/bench_overhead.py

The SDK runs as in production, sending to a local fake ingest from its background thread,
so the numbers include contention with the sender. ``track()`` times are whole blocks, not
differences from a baseline, so they overstate the true overhead slightly. The openai and
anthropic numbers are differences from the same call or iteration without the SDK.
"""

from __future__ import annotations

import statistics
import sys
import time
from collections.abc import Callable
from types import ModuleType, SimpleNamespace

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


def call_overhead(lib: ModuleType, operation: str, base_url: str, result: object) -> float:
    """Added time (µs) per patched call: host detection, the Call, reading the usage and the
    recording. The difference between the wrapper and the bare stand-in for the provider."""
    from gaslit import _wrap

    resource = SimpleNamespace(_client=SimpleNamespace(base_url=base_url))

    def provider_call(self: object, **kwargs: object) -> object:
        return result

    hooks = _wrap.Hooks(lib._begin, lib._succeed, lib._fail)
    wrapped = _wrap._sync_call(provider_call, operation, hooks)
    kwargs = {"model": "m-1", "messages": []}
    bare, _ = median_us(lambda: provider_call(resource, **kwargs))
    measured, _ = median_us(lambda: wrapped(resource, **kwargs))
    return measured - bare


def item_overhead(lib: ModuleType, items: list[object], handler: Callable[..., None]) -> float:
    """Added time (µs) per streamed item: one item's check in the measuring iterator."""
    from gaslit import _wrap
    from gaslit._model import Call

    def consume(measure: bool) -> float:
        call = Call("x", "direct", "x", "m", streaming=True)
        it = iter(items)
        if measure:
            it = _wrap.SyncMeasure(call, it, handler, lib._mark_error)
        t0 = time.perf_counter_ns()
        for _ in it:
            pass
        return (time.perf_counter_ns() - t0) / len(items) / 1000

    return statistics.median(consume(True) for _ in range(30)) - statistics.median(
        consume(False) for _ in range(30)
    )


def openai_overhead() -> tuple[float, float, float]:
    """Per ``create`` call, per chat chunk and per Responses event."""
    from openai.types.chat import ChatCompletion, ChatCompletionChunk
    from openai.types.responses import ResponseTextDeltaEvent

    from gaslit import _openai

    completion = ChatCompletion.model_validate(
        {
            "id": "c", "object": "chat.completion", "created": 1, "model": "gpt-4o",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "x"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )  # fmt: skip
    chunk = ChatCompletionChunk.model_validate(
        {
            "id": "c", "object": "chat.completion.chunk", "created": 1, "model": "gpt-4o",
            "choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": None}],
        }
    )  # fmt: skip
    event = ResponseTextDeltaEvent.construct(type="response.output_text.delta", delta="x")
    return (
        call_overhead(_openai, "chat.completions", "https://api.openai.com/v1/", completion),
        item_overhead(_openai, [chunk] * 5000, _openai._chat_item),
        item_overhead(_openai, [event] * 5000, _openai._event_item),
    )


def anthropic_overhead() -> tuple[float, float]:
    """Per ``messages.create`` call and per stream event."""
    from anthropic.types import Message, RawContentBlockDeltaEvent

    from gaslit import _anthropic

    message = Message.model_validate(
        {
            "id": "m", "type": "message", "role": "assistant", "model": "claude-sonnet-4-5",
            "content": [{"type": "text", "text": "x"}], "stop_reason": "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5,
                                             "cache_read_input_tokens": 0,
                                             "cache_creation_input_tokens": 0},
        }
    )  # fmt: skip
    delta = RawContentBlockDeltaEvent.model_validate(
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "x"}}
    )
    return (
        call_overhead(_anthropic, "messages", "https://api.anthropic.com", message),
        item_overhead(_anthropic, [delta] * 5000, _anthropic._event_item),
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
        call_anthropic, anthropic_event = anthropic_overhead()

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
    print(f"  anthropic create  added {call_anthropic:6.2f}  budget {CALL_BUDGET_US}")
    print(f"  anthropic event     {anthropic_event:6.3f}  budget {CHUNK_BUDGET_US}")
    if (
        call_med > CALL_BUDGET_US
        or chunk_med > CHUNK_BUDGET_US
        or max(call_openai, call_anthropic) > CALL_BUDGET_US
        or max(chat_chunk, event_chunk, anthropic_event) > CHUNK_BUDGET_US
    ):
        print("FAIL: over budget")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
