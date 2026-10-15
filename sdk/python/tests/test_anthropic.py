"""The Anthropic integration, driven by the real ``anthropic`` client against the fake provider.

Covers the brief §6.9 matrix (``messages.create`` and the ``messages.stream()`` helper x
sync/async x streaming x success and each error class), errors reported inside a stream,
transparency (P3), fail-open (P4) and cancelled streams. Privacy sentinels are in
``test_anthropic_privacy.py``.
"""

from __future__ import annotations

import asyncio
import gc
import inspect
import time
import weakref
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import anthropic
import pytest
from anthropic.lib.streaming import AsyncMessageStream, MessageStream
from conftest import KEY
from fake_provider import Behavior, FakeProvider

import gaslit
from gaslit import _anthropic, _runtime
from gaslit._model import Call

KW: dict[str, Any] = {
    "model": "claude-test",
    "max_tokens": 10,
    "messages": [{"role": "user", "content": "hello"}],
}
# (way of calling, stream). The helper always streams.
WAYS = [("create", False), ("create", True), ("helper", True)]


@pytest.fixture
def provider() -> Iterator[FakeProvider]:
    with FakeProvider() as fake:
        yield fake


@pytest.fixture
def measured() -> None:
    gaslit.init(api_key=KEY, sample_rate=1.0, max_samples_per_minute=1000)


def clients(
    provider: FakeProvider, **kw: Any
) -> tuple[anthropic.Anthropic, anthropic.AsyncAnthropic]:
    opts = {"base_url": provider.url, "api_key": "sk-ant-fake", "max_retries": 0, **kw}
    return anthropic.Anthropic(**opts), anthropic.AsyncAnthropic(**opts)


def run(way: str, client: Any, is_async: bool, stream: bool, **kw: Any) -> Any:
    """Make the call and, for a stream, read it to the end. Returns the result or the events."""
    kw = {**KW, **kw}
    if is_async:

        async def go() -> Any:
            if way == "helper":
                async with client.messages.stream(**kw) as s:
                    return [e async for e in s]
            result = await client.messages.create(stream=stream, **kw)
            return [e async for e in result] if stream else result

        return asyncio.run(go())
    if way == "helper":
        with client.messages.stream(**kw) as s:
            return list(s)
    result = client.messages.create(stream=stream, **kw)
    return list(result) if stream else result


def cancelled_count() -> int:
    """Cancelled calls are tallied but not sampled, so read the summaries."""
    assert _runtime.state is not None
    return sum(s.cancelled for s in _runtime.state.aggregator.pop_all())


def calls() -> list[Call]:
    assert _runtime.state is not None
    return [s.call for s in _runtime.state.queue.take(1000)]  # type: ignore[union-attr]


MATRIX = pytest.mark.parametrize(
    ("way", "stream", "is_async"),
    [(w, s, a) for w, s in WAYS for a in (False, True)],
    ids=lambda v: str(v),
)


@MATRIX
def test_success(
    measured: None, provider: FakeProvider, way: str, stream: bool, is_async: bool
) -> None:
    sync, aio = clients(provider)
    provider.script(Behavior(latency_ms=40, chunk_ms=5))
    run(way, aio if is_async else sync, is_async, stream)
    [call] = calls()
    assert (call.provider, call.route, call.operation) == ("local", "local", "messages")
    assert (call.endpoint_host, call.status, call.error_class) == (None, "ok", None)
    assert (call.model_requested, call.model_returned) == ("claude-test", "claude-test")
    assert call.streaming is stream
    assert (call.provider_sdk, call.provider_sdk_version) == ("anthropic", anthropic.__version__)
    assert call.duration_ms >= 40
    if stream:
        assert call.ttft_ms is not None and 40 <= call.ttft_ms <= call.duration_ms
    else:
        assert call.ttft_ms is None
    assert (call.input_tokens, call.output_tokens, call.output_tokens_estimated) == (10, 5, False)
    assert call.cached_input_tokens == 0


@MATRIX
def test_cache_reads_count_as_input(
    measured: None, provider: FakeProvider, way: str, stream: bool, is_async: bool
) -> None:
    """Anthropic's input_tokens leaves out cached tokens; the SDK reports the whole prompt."""
    sync, aio = clients(provider)
    provider.script(Behavior(input_tokens=10, cached_tokens=90))
    run(way, aio if is_async else sync, is_async, stream)
    [call] = calls()
    assert (call.input_tokens, call.cached_input_tokens) == (100, 90)


@pytest.mark.parametrize("way", ["create", "helper"])
def test_has_tools_is_a_flag_only(measured: None, provider: FakeProvider, way: str) -> None:
    sync, _ = clients(provider)
    tool = {"name": "f", "description": "d", "input_schema": {"type": "object"}}
    run(way, sync, False, way == "helper", tools=[tool])
    run(way, sync, False, way == "helper")
    with_tools, without = calls()
    assert (with_tools.has_tools, without.has_tools) == (True, False)


ERRORS = pytest.mark.parametrize(
    ("status", "error_class", "code"),
    [
        (429, "rate_limited", "rate_limit_error"),
        (529, "overloaded", "overloaded_error"),
        (503, "server_error", "api_error"),
        (401, "auth_error", "authentication_error"),
        (400, "client_error", "invalid_request_error"),
    ],
)


@ERRORS
@MATRIX
def test_errors(
    measured: None,
    provider: FakeProvider,
    way: str,
    stream: bool,
    is_async: bool,
    status: int,
    error_class: str,
    code: str,
) -> None:
    sync, aio = clients(provider)
    provider.script(status, status)
    with pytest.raises(anthropic.APIStatusError) as measured_exc:
        run(way, aio if is_async else sync, is_async, stream)
    _anthropic.uninstall()
    with pytest.raises(anthropic.APIStatusError) as plain_exc:  # the same call, unmeasured
        run(way, aio if is_async else sync, is_async, stream)
    assert type(measured_exc.value) is type(plain_exc.value)
    assert (measured_exc.value.status_code, str(measured_exc.value)) == (
        plain_exc.value.status_code,
        str(plain_exc.value),
    )
    [call] = calls()
    assert (call.status, call.error_class) == ("error", error_class)
    assert (call.http_status, call.provider_error_code) == (status, code)
    assert call.ttft_ms is None


@pytest.mark.parametrize(
    ("way", "is_async"), [(w, a) for w in ("create", "helper") for a in (0, 1)]
)
@pytest.mark.parametrize(
    ("status", "error_class", "code"),
    [(529, "overloaded", "overloaded_error"), (500, "server_error", "api_error")],
)
def test_an_error_inside_a_stream(
    measured: None,
    provider: FakeProvider,
    way: str,
    is_async: int,
    status: int,
    error_class: str,
    code: str,
) -> None:
    """Anthropic can fail a stream after HTTP 200 with an error event; its type gives the class."""
    sync, aio = clients(provider)
    provider.script(Behavior(stream_error=status, chunks=2))
    with pytest.raises(anthropic.APIStatusError) as caught:
        run(way, aio if is_async else sync, bool(is_async), True)
    assert caught.value.status_code == 200
    [call] = calls()
    assert (call.status, call.error_class) == ("error", error_class)
    assert (call.http_status, call.provider_error_code) == (200, code)
    assert call.ttft_ms is not None  # two tokens arrived before the error
    assert (call.output_tokens, call.output_tokens_estimated) == (None, False)


def test_timeout_and_connection_errors(measured: None, provider: FakeProvider) -> None:
    sync, _ = clients(provider)
    provider.script(Behavior(latency_ms=1500))
    with pytest.raises(anthropic.APITimeoutError):  # per call: older versions default to 600 s
        sync.messages.create(timeout=0.2, **KW)
    dead = anthropic.Anthropic(base_url="http://127.0.0.1:9", api_key="k", max_retries=0)
    with pytest.raises(anthropic.APIConnectionError):
        dead.messages.create(**KW)
    with pytest.raises(anthropic.APIConnectionError), dead.messages.stream(**KW):
        pass
    slow, refused, helper = calls()
    assert (slow.error_class, slow.http_status) == ("timeout", None)
    assert (refused.error_class, refused.http_status) == ("connection_error", None)
    assert (helper.error_class, helper.streaming) == ("connection_error", True)


def test_a_bad_argument_is_not_a_provider_call(measured: None, provider: FakeProvider) -> None:
    sync, _ = clients(provider)
    with pytest.raises(TypeError):
        sync.messages.create(model="claude-test")  # type: ignore[call-overload]
    with pytest.raises(TypeError):
        sync.messages.stream(model="claude-test")  # type: ignore[call-arg]
    assert calls() == []


# --- Streams -----------------------------------------------------------------------------


@pytest.mark.parametrize("way", ["create", "helper"])
def test_ttft_is_the_first_delta_not_message_start(
    measured: None, provider: FakeProvider, way: str
) -> None:
    sync, _ = clients(provider)
    provider.script(Behavior(latency_ms=120, chunk_ms=10))  # message_start goes out at once
    run(way, sync, False, True)
    [call] = calls()
    assert call.ttft_ms is not None and call.ttft_ms >= 120


def test_streams_keep_their_type_and_behaviour(measured: None, provider: FakeProvider) -> None:
    sync, aio = clients(provider)
    stream = sync.messages.create(stream=True, **KW)
    assert isinstance(stream, anthropic.Stream)
    with stream as same:
        assert same is stream
        assert next(iter(stream)).type == "message_start"
    with sync.messages.stream(**KW) as helper:
        assert isinstance(helper, MessageStream)
        assert "".join(helper.text_stream) == "word0 word1 word2 word3 word4 "
        assert helper.get_final_message().usage.output_tokens == 5

    async def go() -> None:
        astream = await aio.messages.create(stream=True, **KW)
        assert isinstance(astream, anthropic.AsyncStream)
        async with astream as same:
            assert same is astream
            assert (await astream.__anext__()).type == "message_start"
        async with aio.messages.stream(**KW) as ahelper:
            assert isinstance(ahelper, AsyncMessageStream)
            assert await ahelper.get_final_text() == "word0 word1 word2 word3 word4 "

    asyncio.run(go())
    assert [c.status for c in calls()].count("ok") == 2  # the helpers, read to the end
    assert cancelled_count() == 2  # the streams left after one event


def test_leaving_a_stream_early_is_cancelled(measured: None, provider: FakeProvider) -> None:
    sync, aio = clients(provider)
    provider.script(*[Behavior(chunks=20, chunk_ms=5)] * 4)
    with sync.messages.create(stream=True, **KW) as stream:
        next(iter(stream))
    closed = sync.messages.create(stream=True, **KW)
    next(iter(closed))
    closed.close()
    with sync.messages.stream(**KW) as helper:
        next(iter(helper))

    async def go() -> None:
        async with aio.messages.stream(**KW) as ahelper:
            await ahelper.__anext__()

    asyncio.run(go())
    assert cancelled_count() == 4


def test_stopping_at_message_stop_is_not_cancelled(measured: None, provider: FakeProvider) -> None:
    sync, _ = clients(provider)
    with sync.messages.stream(**KW) as helper:
        for event in helper:
            if event.type == "message_stop":
                break
    [call] = calls()
    assert (call.status, call.output_tokens) == ("ok", 5)


def test_a_dropped_stream_is_cancelled_and_freed(measured: None, provider: FakeProvider) -> None:
    sync, _ = clients(provider)
    provider.script(Behavior(chunks=20, chunk_ms=5))
    stream = sync.messages.create(stream=True, **KW)
    next(iter(stream))
    ref = weakref.ref(stream)
    del stream
    gc.collect()
    assert ref() is None  # our finalizer must not keep the stream alive
    assert len(_runtime._abandoned) == 1
    sync.messages.create(**KW)  # the next call records the abandoned one
    assert cancelled_count() == 1


def test_cancelling_an_async_task_mid_stream(measured: None, provider: FakeProvider) -> None:
    _, aio = clients(provider)
    provider.script(Behavior(chunks=50, chunk_ms=20))

    async def go() -> None:
        started = asyncio.Event()

        async def consume() -> None:
            async with aio.messages.stream(**KW) as stream:
                async for _ in stream:
                    started.set()

        task = asyncio.create_task(consume())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert cancelled_count() == 1


def test_a_helper_that_is_never_entered_is_not_a_call(
    measured: None, provider: FakeProvider
) -> None:
    sync, _ = clients(provider)
    sync.messages.stream(**KW)  # builds the manager; no request is sent until it is entered
    assert calls() == []
    assert provider.calls == []


def test_the_helper_clock_starts_when_it_is_entered(measured: None, provider: FakeProvider) -> None:
    sync, _ = clients(provider)
    manager = sync.messages.stream(**KW)
    time.sleep(0.2)
    with manager as stream:
        stream.until_done()
    [call] = calls()
    assert call.duration_ms < 200


# --- Transparency (P3) --------------------------------------------------------------------


def test_results_and_signatures_are_unchanged(measured: None, provider: FakeProvider) -> None:
    from anthropic.lib.streaming import AsyncMessageStreamManager, MessageStreamManager
    from anthropic.resources.messages import AsyncMessages, Messages

    sync, _ = clients(provider)
    assert isinstance(sync.messages.create(**KW), anthropic.types.Message)
    assert isinstance(sync.messages.stream(**KW), MessageStreamManager)
    patched = [
        (Messages, "create"), (AsyncMessages, "create"), (Messages, "stream"),
        (AsyncMessages, "stream"), (MessageStreamManager, "__enter__"),
        (AsyncMessageStreamManager, "__aenter__"), (anthropic.Stream, "close"),
        (anthropic.AsyncStream, "close"), (MessageStream, "close"), (AsyncMessageStream, "close"),
    ]  # fmt: skip
    for cls, name in patched:
        wrapper: Any = getattr(cls, name)
        original = wrapper._gaslit_original
        assert wrapper.__name__ == name
        assert inspect.signature(wrapper) == inspect.signature(original)
        assert inspect.iscoroutinefunction(wrapper) == inspect.iscoroutinefunction(original)


def test_install_twice_wraps_once_and_uninstall_restores(measured: None) -> None:
    from anthropic.lib.streaming import MessageStreamManager
    from anthropic.resources.messages import Messages

    wrapped = Messages.create, Messages.stream, MessageStreamManager.__enter__
    _anthropic.install()
    assert (Messages.create, Messages.stream, MessageStreamManager.__enter__) == wrapped
    _anthropic.uninstall()
    for method in (Messages.create, Messages.stream, MessageStreamManager.__enter__):
        assert not hasattr(method, "_gaslit_original")


def test_raw_response_calls_are_counted_without_usage(
    measured: None, provider: FakeProvider
) -> None:
    sync, _ = clients(provider)
    raw = sync.messages.with_raw_response.create(**KW)
    assert raw.parse().content  # the user's call still works
    [call] = calls()
    assert (call.status, call.input_tokens, call.output_tokens) == ("ok", None, None)


def test_instrument_choices() -> None:
    from anthropic.resources.messages import Messages

    def patched() -> bool:
        return hasattr(Messages.create, "_gaslit_original")

    gaslit.init(api_key=KEY, instrument=["openai"])
    assert not patched()
    _runtime.reset()
    gaslit.init(api_key=KEY, instrument=["anthropic"])
    assert patched()


def test_kill_switch_means_no_patching(monkeypatch: pytest.MonkeyPatch) -> None:
    from anthropic.resources.messages import Messages

    monkeypatch.setenv("GASLIT_ENABLED", "false")
    gaslit.init(api_key=KEY)
    assert not hasattr(Messages.create, "_gaslit_original")


# --- Hosts and routes ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "model", "expected"),
    [
        ("https://api.anthropic.com", "claude-sonnet-4-5",
         ("anthropic", "direct", "api.anthropic.com", None, "claude-sonnet-4-5")),
        ("https://bedrock-runtime.eu-central-1.amazonaws.com",
         "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
         ("anthropic", "bedrock", "bedrock-runtime.eu-central-1.amazonaws.com", "eu-central-1",
          "eu.anthropic.claude-sonnet-4-5-20250929-v1:0")),
        # An ARN carries the AWS account id: only a public model id is kept from it.
        ("https://bedrock-runtime.us-east-1.amazonaws.com",
         "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-haiku-4-5",
         ("anthropic", "bedrock", "bedrock-runtime.us-east-1.amazonaws.com", "us-east-1",
          "us.anthropic.claude-haiku-4-5")),
        ("https://bedrock-runtime.us-east-1.amazonaws.com",
         "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/a1b2c3",
         ("custom", "bedrock", "bedrock-runtime.us-east-1.amazonaws.com", "us-east-1",
          "bedrock-arn")),
        ("https://us-east5-aiplatform.googleapis.com/v1", "claude-sonnet-4-5@20250929",
         ("anthropic", "vertex", "us-east5-aiplatform.googleapis.com", "us-east5",
          "claude-sonnet-4-5@20250929")),
        # Microsoft Foundry: the resource and deployment names are the customer's.
        ("https://acme.services.ai.azure.com/anthropic/", "acme-claude",
         ("anthropic", "azure", "services.ai.azure.com", None, "azure-deployment")),
        ("https://llm-gateway.example.com/v1", "claude-sonnet-4-5",
         ("custom", "custom", None, None, "claude-sonnet-4-5")),
    ],
)  # fmt: skip
def test_hosts_and_routes(url: str, model: str, expected: tuple[object, ...]) -> None:
    gaslit.init(api_key=KEY)
    resource = SimpleNamespace(_client=SimpleNamespace(base_url=url))
    call = _anthropic._begin(resource, "messages", {"model": model})
    assert call is not None
    got = (call.provider, call.route, call.endpoint_host, call.endpoint_region)
    assert (*got, call.model_requested) == expected


# --- Fail open (P4) -----------------------------------------------------------------------


# (what to break, when the call path reaches it as (way, streaming))
FAULTS = [
    ("gaslit._runtime.record", lambda way, stream: True),
    ("gaslit._model.Call.finish", lambda way, stream: True),
    ("gaslit._anthropic.detect", lambda way, stream: True),
    ("gaslit._anthropic._read_message", lambda way, stream: True),
    ("gaslit._anthropic._read_usage", lambda way, stream: True),
    ("gaslit._anthropic._event_item", lambda way, stream: stream),
    ("gaslit._anthropic._now_ms", lambda way, stream: way == "helper"),
    ("gaslit._wrap.measure", lambda way, stream: stream),
    ("gaslit._model.Call.first_token", lambda way, stream: stream),
]


@pytest.mark.parametrize("fault", FAULTS, ids=lambda f: f[0])
@MATRIX
def test_sdk_faults_never_reach_the_user(
    measured: None,
    provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
    fault: tuple[str, Callable[[str, bool], bool]],
    way: str,
    stream: bool,
    is_async: bool,
) -> None:
    sync, aio = clients(provider)
    client = aio if is_async else sync
    expected = run(way, client, is_async, stream)
    hits = []

    def boom(*_: object, **__: object) -> Any:
        hits.append(1)
        raise RuntimeError("SDK bug")

    monkeypatch.setattr(fault[0], boom)
    got = run(way, client, is_async, stream)
    assert bool(hits) == fault[1](way, stream)  # the fault really was reached
    assert type(got) is type(expected)
    if stream:
        assert [type(e) for e in got] == [type(e) for e in expected]
    else:
        assert got.model_dump(exclude={"id"}) == expected.model_dump(exclude={"id"})


def test_calls_after_shutdown_pass_straight_through(provider: FakeProvider) -> None:
    gaslit.init(api_key=KEY)
    sync, _ = clients(provider)
    manager = sync.messages.stream(**KW)  # noted before shutdown, entered after
    gaslit.shutdown()
    assert sync.messages.create(**KW).content
    with manager as stream:
        stream.until_done()
    assert _runtime.state is None
