"""Call measurement model and wire-format constants (brief §6.4, §6.5, §19.2, ADR 0003).

Every field is metadata. Nothing here can hold prompt, output, key or URL text.
"""

from __future__ import annotations

import time
import uuid
from bisect import bisect_left
from dataclasses import dataclass, field
from typing import NamedTuple

SCHEMA_VERSION = 1

# Upper bounds of the fixed histogram buckets; each histogram has one more bucket for +inf.
# The bounds are part of the protocol: changing them needs a new SCHEMA_VERSION.
LATENCY_BOUNDS_MS = (
    25, 50, 100, 200, 300, 500, 750, 1000, 1500, 2000,
    3000, 5000, 7500, 10000, 15000, 20000, 30000, 60000, 120000,
)  # fmt: skip
DECODE_BOUNDS_TPS = (5, 10, 20, 30, 40, 60, 80, 100, 150, 200, 300, 500, 1000)
LATENCY_BUCKETS = len(LATENCY_BOUNDS_MS) + 1
DECODE_BUCKETS = len(DECODE_BOUNDS_TPS) + 1

# Decode rate is only meaningful for streams with enough real (not estimated) tokens.
DECODE_MIN_TOKENS = 16

OK, ERROR, CANCELLED = "ok", "error", "cancelled"

_QUOTA_CODES = frozenset({"insufficient_quota"})
_OVERLOADED_CODES = frozenset({"overloaded_error"})
_STATUS_CLASSES = {
    401: "auth_error",
    403: "auth_error",
    408: "timeout",
    429: "rate_limited",
    529: "overloaded",
}


def bucket(bounds: tuple[int, ...], value: float) -> int:
    """Index of the bucket holding ``value``. Buckets are upper-inclusive (value <= bound)."""
    return bisect_left(bounds, value)


def input_bucket(input_tokens: int | None) -> str:
    if input_tokens is None:
        return "unknown"
    if input_tokens < 1_000:
        return "xs"
    if input_tokens < 10_000:
        return "s"
    return "l"


def classify_http(http_status: int | None, code: str | None) -> str:
    """Map an HTTP status and short error code to an ``error_class``. Never reads message text."""
    if code in _QUOTA_CODES:
        return "quota_error"
    if code in _OVERLOADED_CODES:
        return "overloaded"
    if http_status is None:
        return "unknown"
    known = _STATUS_CLASSES.get(http_status)
    if known is not None:
        return known
    if 400 <= http_status < 500:
        return "client_error"
    if 500 <= http_status < 600:
        return "server_error"
    return "unknown"


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class Tags(NamedTuple):
    """Per-process tags from ``init()``. Constant for the process, so not part of the series key."""

    service: str | None = None
    environment: str | None = None
    origin_region: str | None = None


@dataclass(slots=True, eq=False)
class Call:
    """One measured call. Created at call start, filled in as the call progresses."""

    provider: str
    route: str
    operation: str
    model_requested: str
    streaming: bool = False
    endpoint_host: str | None = None
    endpoint_region: str | None = None
    has_tools: bool = False
    label: str | None = None
    provider_sdk: str | None = None
    provider_sdk_version: str | None = None
    ts_ms: int = field(default_factory=_now_ms)
    start_ns: int = field(default_factory=time.perf_counter_ns)
    ttft_ms: float | None = None
    duration_ms: float = 0.0
    model_returned: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens_estimated: bool = False
    status: str = OK
    error_class: str | None = None
    http_status: int | None = None
    provider_error_code: str | None = None

    def first_token(self) -> None:
        """Record time to first output chunk. Only the first call counts."""
        if self.ttft_ms is None:
            self.ttft_ms = (time.perf_counter_ns() - self.start_ns) / 1e6

    def finish(self) -> None:
        self.duration_ms = (time.perf_counter_ns() - self.start_ns) / 1e6

    @property
    def model(self) -> str:
        """The model a summary is keyed by: as returned by the provider, else as requested."""
        return self.model_returned or self.model_requested

    def end_minute_ms(self) -> int:
        """Start of the minute the call finished in. Calls are counted where they finish."""
        end = self.ts_ms + int(self.duration_ms)
        return end - end % 60_000

    def decode_tps(self) -> float | None:
        """Output tokens per second after the first token, or None where it isn't meaningful."""
        if (
            self.status != OK
            or self.ttft_ms is None
            or self.output_tokens is None
            or self.output_tokens < DECODE_MIN_TOKENS
            or self.output_tokens_estimated
            or self.duration_ms <= self.ttft_ms
        ):
            return None
        return self.output_tokens / ((self.duration_ms - self.ttft_ms) / 1000)


def sample_record(call: Call, reason: str, tags: Tags) -> dict[str, object]:
    """Serialise a call as a sample record (brief §6.4)."""
    return {
        "event_id": str(uuid.uuid4()),
        "reason": reason,
        "ts": call.ts_ms,
        "duration_ms": round(call.duration_ms, 1),
        "ttft_ms": None if call.ttft_ms is None else round(call.ttft_ms, 1),
        "provider": call.provider,
        "route": call.route,
        "endpoint_host": call.endpoint_host,
        "endpoint_region": call.endpoint_region,
        "operation": call.operation,
        "model_requested": call.model_requested,
        "model_returned": call.model_returned,
        "streaming": call.streaming,
        "has_tools": call.has_tools,
        "input_tokens": call.input_tokens,
        "output_tokens": call.output_tokens,
        "cached_input_tokens": call.cached_input_tokens,
        "output_tokens_estimated": call.output_tokens_estimated,
        "status": call.status,
        "error_class": call.error_class,
        "http_status": call.http_status,
        "provider_error_code": call.provider_error_code,
        "service": tags.service,
        "environment": tags.environment,
        "label": call.label,
        "origin_region": tags.origin_region,
        "provider_sdk": call.provider_sdk,
        "provider_sdk_version": call.provider_sdk_version,
    }
