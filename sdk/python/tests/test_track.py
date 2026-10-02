import asyncio
import logging

import pytest
from conftest import KEY

import yourpkg
from yourpkg import _runtime
from yourpkg._aggregator import Sample, Summary


def summaries() -> list[Summary]:
    assert _runtime.state is not None
    return _runtime.state.aggregator.pop_all()


def samples() -> list[Sample]:
    assert _runtime.state is not None
    return [s for s in _runtime.state.queue.take(1000) if isinstance(s, Sample)]


def test_track_records_a_streaming_call() -> None:
    yourpkg.init(api_key=KEY, service="svc")
    with yourpkg.track(provider="custom", model="my-finetune", streaming=True) as t:
        t.first_token()
        t.first_token()
        t.set_usage(input_tokens=1234, output_tokens=210, cached_input_tokens=0)
    [s] = summaries()
    assert (s.count, s.ok) == (1, 1)
    assert s.key == ("custom", "custom", None, None, "manual", "my-finetune", True, "s")
    assert sum(s.ttft_hist) == 1
    [sample] = samples()
    assert sample.reason == "first_of_series"
    call = sample.call
    assert (call.input_tokens, call.output_tokens, call.cached_input_tokens) == (1234, 210, 0)
    assert call.ttft_ms is not None and call.ttft_ms <= call.duration_ms


def test_exception_is_recorded_and_reraised_unchanged() -> None:
    yourpkg.init(api_key=KEY)
    err = TimeoutError("SENTINEL_OUTPUT_91c2")
    with pytest.raises(TimeoutError) as info, yourpkg.track(provider="custom", model="m"):
        raise err
    assert info.value is err
    [sample] = samples()
    assert (sample.reason, sample.call.status, sample.call.error_class) == (
        "error",
        "error",
        "timeout",
    )


@pytest.mark.parametrize(
    ("exc", "cls"),
    [(ConnectionResetError(), "connection_error"), (ValueError(), "unknown")],
)
def test_exception_classes(exc: Exception, cls: str) -> None:
    yourpkg.init(api_key=KEY)
    with pytest.raises(type(exc)), yourpkg.track(provider="custom", model="m"):
        raise exc
    assert summaries()[0].errors == {cls: 1}


@pytest.mark.parametrize(
    "exc", [KeyboardInterrupt(), asyncio.CancelledError(), GeneratorExit(), SystemExit()]
)
def test_base_exceptions_count_as_cancelled(exc: BaseException) -> None:
    yourpkg.init(api_key=KEY)
    with pytest.raises(type(exc)), yourpkg.track(provider="custom", model="m"):
        raise exc
    [s] = summaries()
    assert (s.cancelled, s.errors) == (1, {})


def test_set_error_without_raising() -> None:
    yourpkg.init(api_key=KEY)
    with yourpkg.track(provider="openai", model="gpt-x", route="direct") as t:
        t.set_error(http_status=429, code="insufficient_quota")
    [sample] = samples()
    call = sample.call
    assert (call.status, call.error_class, call.http_status, call.provider_error_code) == (
        "error",
        "quota_error",
        429,
        "insufficient_quota",
    )


def test_set_error_wins_over_exception_type() -> None:
    yourpkg.init(api_key=KEY)
    with pytest.raises(RuntimeError), yourpkg.track(provider="custom", model="m") as t:
        t.set_error(http_status=503)
        raise RuntimeError
    assert summaries()[0].errors == {"server_error": 1}


def test_set_error_drops_invalid_values() -> None:
    yourpkg.init(api_key=KEY)
    with yourpkg.track(provider="custom", model="m") as t:
        t.set_error(http_status=1000, code="Error: SENTINEL_PROMPT_7f3a")
    call = samples()[0].call
    assert (call.http_status, call.provider_error_code, call.error_class) == (None, None, "unknown")


def test_route_defaults_and_validation() -> None:
    yourpkg.init(api_key=KEY)
    for route in (None, "bedrock", "nonsense"):
        with yourpkg.track(provider="custom", model="m", route=route):
            pass
    with yourpkg.track(provider="local", model="m"):
        pass
    assert {s.key[1] for s in summaries()} == {"custom", "bedrock", "local"}


def test_bad_arguments_are_normalised_not_raised() -> None:
    yourpkg.init(api_key=KEY)
    with yourpkg.track(
        provider="my-corp",
        model="has spaces\nSENTINEL",
        label="bad label",
        streaming="yes",  # type: ignore[arg-type]
    ) as t:
        t.set_usage(input_tokens="12", output_tokens=-1, cached_input_tokens=True)  # type: ignore[arg-type]
    call = samples()[0].call
    assert (call.provider, call.model_requested, call.label, call.streaming) == (
        "custom",
        "invalid",
        None,
        False,
    )
    assert (call.input_tokens, call.output_tokens, call.cached_input_tokens) == (None, None, None)


def test_unhashable_provider_gives_noop_tracker() -> None:
    yourpkg.init(api_key=KEY)
    with yourpkg.track(provider=[], model="m") as t:  # type: ignore[arg-type]
        t.first_token()
    assert summaries() == []


def test_track_before_init_is_a_noop() -> None:
    with yourpkg.track(provider="custom", model="m") as t:
        t.first_token()
        t.set_usage(input_tokens=1)
        t.set_error(http_status=500)
    assert _runtime.state is None


def test_init_is_idempotent() -> None:
    yourpkg.init(api_key=KEY, service="first")
    first = _runtime.state
    yourpkg.init(api_key=KEY, service="second")
    assert _runtime.state is first
    assert first is not None and first.config.tags.service == "first"


def test_no_key_warns_once_and_does_nothing(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="yourpkg")
    yourpkg.init()
    assert _runtime.state is None
    assert [r.message for r in caplog.records].count(
        "No ingest key: set YOURPKG_KEY or pass api_key. Nothing will be sent."
    ) == 1


def test_no_key_with_debug_still_measures() -> None:
    yourpkg.init(debug=True)
    state = _runtime.state
    assert state is not None and not state.config.can_send


def test_kill_switch_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOURPKG_ENABLED", "false")
    yourpkg.init(api_key=KEY)
    assert _runtime.state is None


def test_init_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**_: object) -> None:
        raise RuntimeError("internal fault")

    monkeypatch.setattr("yourpkg._runtime.load_config", boom)
    yourpkg.init(api_key=KEY)
    assert _runtime.state is None


def test_queue_overflow_drops_and_counts() -> None:
    yourpkg.init(api_key=KEY, max_queue=1)
    for i in range(3):
        with yourpkg.track(provider="custom", model=f"m{i}"):
            pass
    state = _runtime.state
    assert state is not None
    assert len(state.queue) == 1
    assert state.queue.take_dropped() == 2


def test_cardinality_overflow_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="yourpkg")
    yourpkg.init(api_key=KEY)
    for i in range(502):
        with yourpkg.track(provider="custom", model=f"m{i}"):
            pass
    assert sum("model='other'" in r.message for r in caplog.records) == 1
