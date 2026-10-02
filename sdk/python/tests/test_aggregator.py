import random
import threading

from yourpkg._aggregator import OTHER_MODEL, Aggregator
from yourpkg._model import Call, Tags

MINUTE = 1_790_000_040_000

SUMMARY_FIELDS = {
    "summary_id", "minute", "series", "count", "ok", "cancelled", "errors", "ttft_hist",
    "duration_hist", "decode_hist", "input_tokens_sum", "output_tokens_sum",
    "output_tokens_estimated_count",
}  # fmt: skip
SERIES_FIELDS = {
    "provider", "route", "endpoint_host", "endpoint_region", "operation", "model", "streaming",
    "input_bucket", "service", "environment", "origin_region",
}  # fmt: skip


class FixedRng(random.Random):
    def __init__(self, value: float) -> None:
        super().__init__()
        self.value = value

    def random(self) -> float:
        return self.value


def call(**kw: object) -> Call:
    base: dict[str, object] = {
        "provider": "anthropic",
        "route": "direct",
        "operation": "messages",
        "model_requested": "m",
        "ts_ms": MINUTE,
        "duration_ms": 400.0,
    }
    return Call(**(base | kw))  # type: ignore[arg-type]


def agg(
    rate: float = 0.0, cap: int = 10, max_series: int = 500, max_error_samples: int = 100
) -> Aggregator:
    return Aggregator(rate, cap, FixedRng(0.0), max_series, max_error_samples)


def test_summary_tallies_and_histogram_membership() -> None:
    a = agg()
    same: dict[str, object] = {"streaming": True, "input_tokens": 500}
    stream = same | {"ttft_ms": 250.0, "duration_ms": 1100.0}
    fail = same | {"status": "error", "duration_ms": 30.0}
    a.add(call(**stream, output_tokens=170))  # ok: all three hists
    a.add(call(**stream, status="cancelled"))  # ttft only
    a.add(call(**fail, error_class="overloaded"))
    a.add(call(**fail))  # no class -> unknown
    [s] = a.pop_all()
    assert (s.count, s.ok, s.cancelled) == (4, 1, 1)
    assert s.errors == {"overloaded": 1, "unknown": 1}
    assert sum(s.duration_hist) == 1  # ok calls only (ADR 0003)
    assert s.duration_hist[8] == 1  # 1000 < 1100 <= 1500
    assert sum(s.ttft_hist) == 2 and s.ttft_hist[4] == 2  # 200 < 250 <= 300
    assert sum(s.decode_hist) == 1 and s.decode_hist[9] == 1  # 150 < 200 tok/s <= 200
    assert (s.input_tokens_sum, s.output_tokens_sum) == (2000, 170)


def test_series_split_by_minute_bucket_and_returned_model() -> None:
    a = agg()
    a.add(call())
    a.add(call(ts_ms=MINUTE + 60_000))
    a.add(call(input_tokens=50_000))
    a.add(call(model_returned="m-2026"))
    keys = {(s.minute, s.key[5], s.key[7]) for s in a.pop_all()}
    assert keys == {
        (MINUTE, "m", "unknown"),
        (MINUTE + 60_000, "m", "unknown"),
        (MINUTE, "m", "l"),
        (MINUTE, "m-2026", "unknown"),
    }


def test_cardinality_guard_folds_into_other() -> None:
    a = agg(max_series=3)
    for i in range(6):
        a.add(call(model_requested=f"m{i}"))
    a.add(call(model_requested="m0"))  # existing series still counted normally
    by_model = {s.key[5]: s.count for s in a.pop_all()}
    assert by_model == {"m0": 2, "m1": 1, "m2": 1, OTHER_MODEL: 3}
    assert a.folded == 3


def test_guard_is_per_minute() -> None:
    a = agg(max_series=1)
    a.add(call(model_requested="a"))
    a.add(call(model_requested="b", ts_ms=MINUTE + 60_000))
    assert {s.key[5] for s in a.pop_all()} == {"a", "b"}
    assert a.folded == 0


def test_first_of_series_once_per_process() -> None:
    a = agg()
    assert a.add(call()) == "first_of_series"
    assert a.add(call()) is None
    assert a.add(call(ts_ms=MINUTE + 60_000)) is None
    assert a.add(call(model_requested="n")) == "first_of_series"


def test_first_call_that_errors_is_sent_as_error_and_marks_series_seen() -> None:
    a = agg()
    assert a.add(call(status="error", error_class="auth_error")) == "error"
    assert a.add(call()) is None


def test_error_samples_capped_per_minute() -> None:
    a = agg(max_error_samples=2)
    err: dict[str, object] = {"status": "error", "error_class": "server_error"}
    assert [a.add(call(**err)) for _ in range(3)] == ["error", "error", None]
    assert a.add(call(ts_ms=MINUTE + 60_000, **err)) == "error"
    [s0, _] = sorted(a.pop_all(), key=lambda s: s.minute)
    assert s0.errors == {"server_error": 3}  # capped samples are still counted


def test_random_samples_respect_rate_and_cap() -> None:
    a = agg(rate=1.0, cap=2)
    a.add(call())  # first_of_series
    assert [a.add(call()) for _ in range(3)] == ["random", "random", None]
    assert a.add(call(ts_ms=MINUTE + 60_000)) == "random"


def test_random_samples_only_ok_calls() -> None:
    a = agg(rate=1.0)
    a.add(call())
    assert a.add(call(status="cancelled")) is None


def test_no_random_samples_when_rng_above_rate() -> None:
    a = Aggregator(0.05, 10, rng=FixedRng(0.06))
    a.add(call())
    assert all(a.add(call()) is None for _ in range(50))


def test_pop_closed_leaves_current_minute() -> None:
    a = agg()
    a.add(call())
    a.add(call(ts_ms=MINUTE + 60_000))
    assert [s.minute for s in a.pop_closed(MINUTE + 60_000)] == [MINUTE]
    assert a.pop_closed(MINUTE + 60_000) == []
    assert [s.minute for s in a.pop_all()] == [MINUTE + 60_000]
    assert a.pop_all() == []


def test_summary_record_shape() -> None:
    a = agg()
    a.add(call(endpoint_host="api.anthropic.com"))
    [s] = a.pop_all()
    rec = s.record(Tags("summariser", "prod", "fra"))
    assert set(rec) == SUMMARY_FIELDS
    series = rec["series"]
    assert isinstance(series, dict)
    assert set(series) == SERIES_FIELDS
    assert series["service"] == "summariser"
    assert series["endpoint_host"] == "api.anthropic.com"
    assert rec["minute"] == MINUTE


def test_concurrent_adds_are_all_counted() -> None:
    a = agg()

    def work() -> None:
        for _ in range(2000):
            a.add(call())

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(s.count for s in a.pop_all()) == 16_000
