import math

import pytest

from yourpkg._model import (
    DECODE_BOUNDS_TPS,
    DECODE_BUCKETS,
    LATENCY_BOUNDS_MS,
    LATENCY_BUCKETS,
    Call,
    Tags,
    bucket,
    classify_http,
    input_bucket,
    sample_record,
)

# Field list from brief §6.4. The sample record must have exactly these keys.
SAMPLE_FIELDS = {
    "event_id", "reason", "ts", "duration_ms", "ttft_ms", "provider", "route",
    "endpoint_host", "endpoint_region", "operation", "model_requested", "model_returned",
    "streaming", "has_tools", "input_tokens", "output_tokens", "cached_input_tokens",
    "output_tokens_estimated", "status", "error_class", "http_status", "provider_error_code",
    "service", "environment", "label", "origin_region", "provider_sdk", "provider_sdk_version",
}  # fmt: skip


def make_call(**kw: object) -> Call:
    base: dict[str, object] = {
        "provider": "custom",
        "route": "custom",
        "operation": "manual",
        "model_requested": "m",
    }
    return Call(**(base | kw))  # type: ignore[arg-type]


def test_histogram_sizes_match_protocol() -> None:
    assert LATENCY_BUCKETS == 20
    assert DECODE_BUCKETS == 14


@pytest.mark.parametrize(
    ("value", "index"),
    [(0, 0), (25, 0), (25.01, 1), (50, 1), (999.9, 7), (1000, 7), (120_000, 18),
     (120_000.1, 19), (math.inf, 19)],
)  # fmt: skip
def test_latency_buckets_are_upper_inclusive(value: float, index: int) -> None:
    assert bucket(LATENCY_BOUNDS_MS, value) == index


def test_decode_buckets() -> None:
    assert bucket(DECODE_BOUNDS_TPS, 5) == 0
    assert bucket(DECODE_BOUNDS_TPS, 1000) == 12
    assert bucket(DECODE_BOUNDS_TPS, 1001) == 13


@pytest.mark.parametrize(
    ("tokens", "name"),
    [(None, "unknown"), (0, "xs"), (999, "xs"), (1000, "s"), (9999, "s"), (10_000, "l")],
)
def test_input_bucket(tokens: int | None, name: str) -> None:
    assert input_bucket(tokens) == name


@pytest.mark.parametrize(
    ("status", "code", "cls"),
    [
        (429, None, "rate_limited"),
        (429, "rate_limit_exceeded", "rate_limited"),
        (429, "insufficient_quota", "quota_error"),
        (401, None, "auth_error"),
        (403, None, "auth_error"),
        (400, None, "client_error"),
        (404, None, "client_error"),
        (422, None, "client_error"),
        (418, None, "client_error"),
        (408, None, "timeout"),
        (529, None, "overloaded"),
        (503, "overloaded_error", "overloaded"),
        (503, None, "server_error"),
        (500, None, "server_error"),
        (599, None, "server_error"),
        (None, None, "unknown"),
        (302, None, "unknown"),
    ],
)
def test_classify_http(status: int | None, code: str | None, cls: str) -> None:
    assert classify_http(status, code) == cls


def test_first_token_is_idempotent() -> None:
    call = make_call(streaming=True)
    call.first_token()
    first = call.ttft_ms
    assert first is not None
    call.first_token()
    assert call.ttft_ms == first


def test_finish_measures_duration() -> None:
    call = make_call()
    call.finish()
    assert call.duration_ms >= 0


def test_model_prefers_returned() -> None:
    assert make_call().model == "m"
    assert make_call(model_returned="m-2026").model == "m-2026"


def test_counted_in_minute_call_finishes() -> None:
    call = make_call(ts_ms=59_000, duration_ms=1500.0)
    assert call.end_minute_ms() == 60_000


def test_decode_tps() -> None:
    call = make_call(streaming=True, ttft_ms=100.0, duration_ms=1100.0, output_tokens=50)
    assert call.decode_tps() == 50.0
    for change in (
        {"output_tokens": 15},
        {"output_tokens": None},
        {"output_tokens_estimated": True},
        {"ttft_ms": None},
        {"duration_ms": 100.0},
        {"status": "error"},
    ):
        bad = make_call(**({"ttft_ms": 100.0, "duration_ms": 1100.0, "output_tokens": 50} | change))
        assert bad.decode_tps() is None, change


def test_sample_record_has_exactly_the_brief_fields() -> None:
    call = make_call(ts_ms=1_790_000_000_000, duration_ms=2843.14, ttft_ms=612.44)
    rec = sample_record(call, "random", Tags("svc", "prod", "fra"))
    assert set(rec) == SAMPLE_FIELDS
    assert rec["duration_ms"] == 2843.1
    assert rec["ttft_ms"] == 612.4
    assert (rec["service"], rec["environment"], rec["origin_region"]) == ("svc", "prod", "fra")
    assert rec["event_id"] != sample_record(call, "random", Tags())["event_id"]
