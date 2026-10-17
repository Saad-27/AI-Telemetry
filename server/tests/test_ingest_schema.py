"""Record rules of the wire format (brief §6.4, §19.2, ADR 0016)."""

from typing import Any

import pytest
from records import Record, envelope, now_ms, sample, summary

from app.ingest.schema import FUTURE_MS, MAX_ERRORS, PAST_MS, Envelope, Records, validate_records

SENTINEL = "SENTINEL-prompt-text"


def check(summaries: list[Any] | None = None, samples: list[Any] | None = None) -> Records:
    return validate_records(Envelope.model_validate(envelope(summaries, samples)), now_ms())


def reason_for_sample(**changes: Any) -> str:
    result = check(samples=[sample(**changes)])
    assert result.rejected == 1, changes
    return str(result.errors[0]["reason"])


def reason_for_summary(record: Record) -> str:
    result = check(summaries=[record])
    assert result.rejected == 1
    return str(result.errors[0]["reason"])


def test_sdk_shaped_records_pass() -> None:
    result = check([summary()], [sample(), sample(status="cancelled", error_class="cancelled")])
    assert (len(result.summaries), len(result.samples), result.rejected) == (1, 2, 0)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"reason": "because"}, "reason: literal_error"),
        ({"provider": "acme"}, "provider: literal_error"),
        ({"model_requested": "gpt 4"}, "model_requested: string_pattern_mismatch"),
        ({"model_requested": "a" * 129}, "model_requested: string_pattern_mismatch"),
        ({"model_returned": "gpt-x\n"}, "model_returned: string_pattern_mismatch"),
        ({"service": "a/b"}, "service: string_pattern_mismatch"),
        ({"provider_error_code": "Rate limit hit"}, "provider_error_code: string_pattern_mismatch"),
        ({"duration_ms": -1}, "duration_ms: greater_than_equal"),
        ({"duration_ms": 3_600_001}, "duration_ms: less_than_equal"),
        ({"ttft_ms": 2845.0}, "ttft_after_end"),
        ({"input_tokens": 10_000_001}, "input_tokens: less_than_equal"),
        ({"input_tokens": 1.0}, "input_tokens: int_type"),
        ({"streaming": "true"}, "streaming: bool_type"),
        ({"http_status": 99}, "http_status: greater_than_equal"),
        ({"error_class": "teapot"}, "error_class: literal_error"),
        ({"event_id": "not-a-uuid"}, "event_id: uuid_parsing"),
        ({"ts": "1790000000000"}, "ts: int_type"),
        ({"endpoint_host": "acme.openai.azure.com"}, "endpoint_host: host_not_allowed"),
        ({"endpoint_host": "api.openai.com.evil.example"}, "endpoint_host: host_not_allowed"),
        ({"endpoint_region": "Frankfurt am Main"}, "endpoint_region: string_pattern_mismatch"),
    ],
)
def test_sample_rules(changes: dict[str, Any], reason: str) -> None:
    assert reason_for_sample(**changes) == reason


def test_ttft_may_exceed_duration_by_rounding() -> None:
    assert check(samples=[sample(duration_ms=100.0, ttft_ms=100.1)]).rejected == 0


@pytest.mark.parametrize(
    "host",
    [
        "api.openai.com",
        "openai.azure.com",
        "bedrock-runtime.eu-central-1.amazonaws.com",
        "bedrock-runtime-fips.us-gov-west-1.amazonaws.com",
        "europe-west4-aiplatform.googleapis.com",
        "aiplatform.eu.rep.googleapis.com",
        "api.together.ai",
    ],
)
def test_allowlisted_hosts(host: str) -> None:
    assert check(samples=[sample(endpoint_host=host)]).rejected == 0


def test_timestamp_window() -> None:
    now = now_ms()
    for ts, ok in (
        (now - PAST_MS, True),
        (now - PAST_MS - 60_000, False),
        (now + FUTURE_MS - 1_000, True),
        (now + FUTURE_MS + 60_000, False),
    ):
        batch = Envelope.model_validate(envelope(samples=[sample(ts=ts)]))
        assert (validate_records(batch, now).rejected == 0) is ok, ts


def test_missing_and_extra_fields() -> None:
    record = sample()
    del record["label"]
    assert check(samples=[record]).errors[0]["reason"] == "label: missing"
    assert reason_for_sample(origin_continent="EU") == "extra_forbidden"


def test_summary_counts_must_add_up() -> None:
    assert reason_for_summary(summary(ok=118)) == "counts_mismatch"
    assert reason_for_summary(summary(errors={"overloaded": 3})) == "counts_mismatch"
    hist = summary()["duration_hist"]
    hist[0] += 1
    assert reason_for_summary(summary(duration_hist=hist)) == "counts_mismatch"
    assert reason_for_summary(summary(input_tokens_sum=120 * 10_000_000 + 1)) == "counts_mismatch"


def test_summary_rules() -> None:
    assert reason_for_summary(summary(minute=summary()["minute"] + 1)) == "minute: not_a_minute"
    assert reason_for_summary(summary(ttft_hist=[0] * 19)) == "ttft_hist: too_short"
    empty = summary(count=0, ok=0, cancelled=0, errors={}, duration_hist=[0] * 20)
    assert reason_for_summary(empty) == "count: greater_than_equal"
    record = summary()
    record["series"]["model"] = "gpt x"
    assert reason_for_summary(record) == "series.model: string_pattern_mismatch"


def test_reasons_never_echo_input() -> None:
    record = summary(errors={SENTINEL: 2})
    weird = sample(**{SENTINEL: 1})
    result = check([record], [weird, sample(model_requested=SENTINEL + " x"), SENTINEL])
    assert result.rejected == 4
    assert [e["reason"] for e in result.errors] == [
        "errors: literal_error",
        "extra_forbidden",
        "model_requested: string_pattern_mismatch",
        "model_type",
    ]
    assert SENTINEL not in repr(result.errors)


def test_sample_input_bucket_is_derived() -> None:
    buckets = [
        check(samples=[sample(input_tokens=n)]).samples[0].input_bucket
        for n in (None, 0, 999, 1_000, 9_999, 10_000)
    ]
    assert buckets == ["unknown", "xs", "xs", "s", "s", "l"]


def test_errors_are_capped_but_all_counted() -> None:
    result = check(samples=[sample(provider="acme") for _ in range(MAX_ERRORS + 5)] + [sample()])
    assert (result.rejected, len(result.errors), len(result.samples)) == (MAX_ERRORS + 5, 20, 1)
    assert result.errors[-1]["index"] == MAX_ERRORS - 1
