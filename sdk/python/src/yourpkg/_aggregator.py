"""Per-minute summary aggregator and sampling decisions (brief §19.3, ADR 0003).

``Aggregator.add`` is on the caller's thread: O(1) work under one short lock.
"""

from __future__ import annotations

import random
import threading
import uuid

from ._model import (
    CANCELLED,
    DECODE_BOUNDS_TPS,
    DECODE_BUCKETS,
    ERROR,
    LATENCY_BOUNDS_MS,
    LATENCY_BUCKETS,
    OK,
    Call,
    Tags,
    bucket,
    input_bucket,
)

# (provider, route, endpoint_host, endpoint_region, operation, model, streaming, input_bucket)
SeriesKey = tuple[str, str, str | None, str | None, str, str, bool, str]

MAX_SERIES = 500
MAX_ERROR_SAMPLES = 100
OTHER_MODEL = "other"


class Summary:
    """One series' tally for one minute (brief §19.2)."""

    __slots__ = (
        "cancelled",
        "count",
        "decode_hist",
        "duration_hist",
        "errors",
        "input_tokens_sum",
        "key",
        "minute",
        "ok",
        "output_tokens_estimated_count",
        "output_tokens_sum",
        "ttft_hist",
    )

    def __init__(self, minute: int, key: SeriesKey) -> None:
        self.minute = minute
        self.key = key
        self.count = 0
        self.ok = 0
        self.cancelled = 0
        self.errors: dict[str, int] = {}
        self.ttft_hist = [0] * LATENCY_BUCKETS
        self.duration_hist = [0] * LATENCY_BUCKETS
        self.decode_hist = [0] * DECODE_BUCKETS
        self.input_tokens_sum = 0
        self.output_tokens_sum = 0
        self.output_tokens_estimated_count = 0

    def add(self, call: Call) -> None:
        self.count += 1
        if call.status == OK:
            self.ok += 1
            # Duration covers successful calls only, so fast failures can't hide a slowdown.
            self.duration_hist[bucket(LATENCY_BOUNDS_MS, call.duration_ms)] += 1
            tps = call.decode_tps()
            if tps is not None:
                self.decode_hist[bucket(DECODE_BOUNDS_TPS, tps)] += 1
        elif call.status == CANCELLED:
            self.cancelled += 1
        else:
            cls = call.error_class or "unknown"
            self.errors[cls] = self.errors.get(cls, 0) + 1
        # TTFT is valid whatever happened after the first token.
        if call.ttft_ms is not None:
            self.ttft_hist[bucket(LATENCY_BOUNDS_MS, call.ttft_ms)] += 1
        if call.input_tokens is not None:
            self.input_tokens_sum += call.input_tokens
        if call.output_tokens is not None:
            self.output_tokens_sum += call.output_tokens
        if call.output_tokens_estimated:
            self.output_tokens_estimated_count += 1

    def record(self, tags: Tags) -> dict[str, object]:
        provider, route, host, region, operation, model, streaming, ibucket = self.key
        return {
            "summary_id": str(uuid.uuid4()),
            "minute": self.minute,
            "series": {
                "provider": provider,
                "route": route,
                "endpoint_host": host,
                "endpoint_region": region,
                "operation": operation,
                "model": model,
                "streaming": streaming,
                "input_bucket": ibucket,
                "service": tags.service,
                "environment": tags.environment,
                "origin_region": tags.origin_region,
            },
            "count": self.count,
            "ok": self.ok,
            "cancelled": self.cancelled,
            "errors": dict(self.errors),
            "ttft_hist": list(self.ttft_hist),
            "duration_hist": list(self.duration_hist),
            "decode_hist": list(self.decode_hist),
            "input_tokens_sum": self.input_tokens_sum,
            "output_tokens_sum": self.output_tokens_sum,
            "output_tokens_estimated_count": self.output_tokens_estimated_count,
        }


class Sample:
    """A call chosen to be sent individually, and why."""

    __slots__ = ("call", "reason")

    def __init__(self, call: Call, reason: str) -> None:
        self.call = call
        self.reason = reason


class Aggregator:
    def __init__(
        self,
        sample_rate: float,
        max_samples_per_minute: int,
        rng: random.Random | None = None,
        max_series: int = MAX_SERIES,
        max_error_samples: int = MAX_ERROR_SAMPLES,
    ) -> None:
        self._sample_rate = sample_rate
        self._max_random = max_samples_per_minute
        self._max_errors = max_error_samples
        self._max_series = max_series
        self._rng = rng or random.Random()  # noqa: S311 (sampling, not security)
        self._lock = threading.Lock()
        self._minutes: dict[int, dict[SeriesKey, Summary]] = {}
        # Series already seen by this process, for first_of_series. Capped like the guard.
        self._seen: set[SeriesKey] = set()
        self._sample_minute = 0
        self._error_samples = 0
        self._random_samples = 0
        self.folded = 0  # calls folded into model="other" by the cardinality guard

    def add(self, call: Call) -> str | None:
        """Tally a finished call. Returns the sample reason if it should also be sent alone."""
        minute = call.end_minute_ms()
        key: SeriesKey = (
            call.provider,
            call.route,
            call.endpoint_host,
            call.endpoint_region,
            call.operation,
            call.model,
            call.streaming,
            input_bucket(call.input_tokens),
        )
        with self._lock:
            cells = self._minutes.get(minute)
            if cells is None:
                cells = self._minutes[minute] = {}
            summary = cells.get(key)
            if summary is None:
                cell_key = key
                if len(cells) >= self._max_series:
                    self.folded += 1
                    p, r, host, region, op, _, streaming, ibucket = key
                    cell_key = (p, r, host, region, op, OTHER_MODEL, streaming, ibucket)
                    summary = cells.get(cell_key)
                if summary is None:
                    summary = cells[cell_key] = Summary(minute, cell_key)
            summary.add(call)
            return self._sample_reason(minute, key, call)

    def _sample_reason(self, minute: int, key: SeriesKey, call: Call) -> str | None:
        if minute > self._sample_minute:
            self._sample_minute = minute
            self._error_samples = self._random_samples = 0
        first = key not in self._seen and len(self._seen) < self._max_series
        if first:
            self._seen.add(key)
        if call.status == ERROR:
            if self._error_samples < self._max_errors:
                self._error_samples += 1
                return "error"
            return None
        if first:
            return "first_of_series"
        if (
            call.status == OK
            and self._random_samples < self._max_random
            and self._rng.random() < self._sample_rate
        ):
            self._random_samples += 1
            return "random"
        return None

    def pop_closed(self, before_minute: int) -> list[Summary]:
        """Remove and return summaries for minutes that start before ``before_minute``."""
        with self._lock:
            done = [m for m in self._minutes if m < before_minute]
            return [s for m in done for s in self._minutes.pop(m).values()]

    def pop_all(self) -> list[Summary]:
        """Remove and return every summary, including the current partial minute.

        Safe because the server sums summaries: a later call in the same minute just
        produces a second summary for that minute and series.
        """
        with self._lock:
            minutes, self._minutes = self._minutes, {}
        return [s for cells in minutes.values() for s in cells.values()]
