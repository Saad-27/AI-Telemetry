"""The OpenAI integration, driven by the real ``openai`` client against the fake provider.

Covers the brief §6.9 matrix (method x sync/async x streaming x success and each error class),
transparency (P3), fail-open (P4) and cancelled streams. Privacy sentinels are in
``test_openai_privacy.py``.
"""

from __future__ import annotations

import asyncio
import gc
import importlib.util
import inspect
import weakref
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import openai
import pytest
from conftest import KEY
from fake_provider import Behavior, FakeProvider

import gaslit
from gaslit import _openai, _runtime
from gaslit._model import Call

PROMPT = "hello"


def chat(c: Any, **kw: Any) -> Any:
    return c.chat.completions.create(
        model="gpt-test", messages=[{"role": "user", "content": PROMPT}], **kw
    )


def responses(c: Any, **kw: Any) -> Any:
    return c.responses.create(model="gpt-test", input=PROMPT, **kw)


OPS = {"chat.completions": chat}
if importlib.util.find_spec("openai.resources.responses"):  # added in openai 1.66
    OPS["responses"] = responses


@pytest.fixture
def provider() -> Iterator[FakeProvider]:
    with FakeProvider() as fake:
        yield fake


@pytest.fixture
def measured() -> None:
    gaslit.init(api_key=KEY, sample_rate=1.0, max_samples_per_minute=1000)


def clients(provider: FakeProvider, **kw: Any) -> tuple[openai.OpenAI, openai.AsyncOpenAI]:
    opts = {"base_url": provider.url + "/v1", "api_key": "sk-fake", "max_retries": 0, **kw}
    return openai.OpenAI(**opts), openai.AsyncOpenAI(**opts)


def run(op: str, client: Any, is_async: bool, stream: bool, **kw: Any) -> Any:
    """Make the call and, for a stream, read it to the end. Returns the result or the chunks."""
    if is_async:

        async def go() -> Any:
            result = await OPS[op](client, stream=stream, **kw)
            return [e async for e in result] if stream else result

        return asyncio.run(go())
    result = OPS[op](client, stream=stream, **kw)
    return list(result) if stream else result


def cancelled_count() -> int:
    """Cancelled calls are tallied but not sampled, so read the summaries."""
    assert _runtime.state is not None
    return sum(s.cancelled for s in _runtime.state.aggregator.pop_all())


def calls() -> list[Call]:
    assert _runtime.state is not None
    return [s.call for s in _runtime.state.queue.take(1000)]  # type: ignore[union-attr]


MATRIX = pytest.mark.parametrize(
    ("op", "is_async", "stream"),
    [(o, a, s) for o in OPS for a in (False, True) for s in (False, True)],
    ids=lambda v: str(v),
)


@MATRIX
def test_success(
    measured: None, provider: FakeProvider, op: str, is_async: bool, stream: bool
) -> None:
    sync, aio = clients(provider)
    provider.script(Behavior(latency_ms=40, chunk_ms=5))
    run(op, aio if is_async else sync, is_async, stream)
    [call] = calls()
    assert (call.provider, call.route, call.operation) == ("local", "local", op)
    assert (call.endpoint_host, call.status, call.error_class) == (None, "ok", None)
    assert (call.model_requested, call.model_returned) == ("gpt-test", "gpt-test")
    assert call.streaming is stream
    assert (call.provider_sdk, call.provider_sdk_version) == ("openai", openai.__version__)
    assert call.duration_ms >= 40
    if stream:
        assert call.ttft_ms is not None and 40 <= call.ttft_ms <= call.duration_ms
    else:
        assert call.ttft_ms is None
    assert call.output_tokens == 5
    # Chat streams only carry usage if the user asks for it, and the SDK never asks (P3).
    if op == "chat.completions" and stream:
        assert (call.input_tokens, call.output_tokens_estimated) == (None, True)
    else:
        assert (call.input_tokens, call.output_tokens_estimated) == (10, False)


@pytest.mark.parametrize("is_async", [False, True])
def test_chat_stream_usage_when_the_user_asks(
    measured: None, provider: FakeProvider, is_async: bool
) -> None:
    sync, aio = clients(provider)
    run("chat.completions", aio if is_async else sync, is_async, True,
        stream_options={"include_usage": True})  # fmt: skip
    [call] = calls()
    assert (call.input_tokens, call.output_tokens, call.output_tokens_estimated) == (10, 5, False)


def test_has_tools_is_a_flag_only(measured: None, provider: FakeProvider) -> None:
    sync, _ = clients(provider)
    tool = {"type": "function", "function": {"name": "f", "parameters": {}}}
    chat(sync, tools=[tool])
    chat(sync)
    with_tools, without = calls()
    assert (with_tools.has_tools, without.has_tools) == (True, False)


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    ("status", "exc_type", "error_class", "code"),
    [
        (429, openai.RateLimitError, "rate_limited", "rate_limit_exceeded"),
        (529, openai.InternalServerError, "overloaded", "overloaded"),
        (503, openai.InternalServerError, "server_error", "server_error"),
        (401, openai.AuthenticationError, "auth_error", "invalid_api_key"),
        (400, openai.BadRequestError, "client_error", "invalid_request"),
    ],
)
def test_errors(
    measured: None,
    provider: FakeProvider,
    op: str,
    is_async: bool,
    stream: bool,
    status: int,
    exc_type: type[Exception],
    error_class: str,
    code: str,
) -> None:
    sync, aio = clients(provider)
    provider.script(status, status)
    with pytest.raises(exc_type) as measured_exc:
        run(op, aio if is_async else sync, is_async, stream)
    _openai.uninstall()
    with pytest.raises(exc_type) as plain_exc:  # the same call, unmeasured
        run(op, aio if is_async else sync, is_async, stream)
    assert (measured_exc.value.status_code, str(measured_exc.value)) == (  # type: ignore[attr-defined]
        plain_exc.value.status_code,  # type: ignore[attr-defined]
        str(plain_exc.value),
    )
    [call] = calls()
    assert (call.status, call.error_class) == ("error", error_class)
    assert (call.http_status, call.provider_error_code) == (status, code)
    assert call.ttft_ms is None


def test_timeout_and_connection_errors(measured: None, provider: FakeProvider) -> None:
    sync, _ = clients(provider, timeout=0.2)
    provider.script(Behavior(latency_ms=1500))
    with pytest.raises(openai.APITimeoutError):
        chat(sync)
    dead = openai.OpenAI(base_url="http://127.0.0.1:9/v1", api_key="k", max_retries=0)
    with pytest.raises(openai.APIConnectionError):
        chat(dead)
    slow, refused = calls()
    assert (slow.error_class, slow.http_status) == ("timeout", None)
    assert (refused.error_class, refused.http_status) == ("connection_error", None)


def test_a_bad_argument_is_not_a_provider_call(measured: None, provider: FakeProvider) -> None:
    sync, _ = clients(provider)
    with pytest.raises(TypeError):
        sync.chat.completions.create(model="gpt-test")  # type: ignore[call-overload]
    assert calls() == []


# --- Streams -----------------------------------------------------------------------------


@pytest.mark.parametrize("op", OPS)
def test_ttft_is_the_first_output_not_the_first_event(
    measured: None, provider: FakeProvider, op: str
) -> None:
    sync, _ = clients(provider)
    provider.script(Behavior(latency_ms=120, chunk_ms=10))
    run(op, sync, False, True)
    [call] = calls()
    assert call.ttft_ms is not None and call.ttft_ms >= 120


@pytest.mark.parametrize("op", OPS)
def test_stream_keeps_its_type_and_behaviour(
    measured: None, provider: FakeProvider, op: str
) -> None:
    sync, aio = clients(provider)
    stream = OPS[op](sync, stream=True)
    assert isinstance(stream, openai.Stream)
    with stream as same:
        assert same is stream
        assert next(iter(stream)) is not None
    assert not isinstance(OPS[op](sync), openai.Stream)

    async def go() -> None:
        astream = await OPS[op](aio, stream=True)
        assert isinstance(astream, openai.AsyncStream)
        async with astream as same:
            assert same is astream
            assert await astream.__anext__() is not None

    asyncio.run(go())


def test_chat_stream_is_not_changed_for_the_user(measured: None, provider: FakeProvider) -> None:
    """P3: no injected include_usage, so no extra chunk with empty choices."""
    sync, _ = clients(provider)
    chunks = run("chat.completions", sync, False, True)
    assert len(chunks) == 7  # role, 5 words, stop
    assert all(c.choices and c.usage is None for c in chunks)
    text = "".join(c.choices[0].delta.content or "" for c in chunks)
    assert text == "word0 word1 word2 word3 word4 "


@pytest.mark.parametrize("op", OPS)
def test_leaving_a_stream_early_is_cancelled(
    measured: None, provider: FakeProvider, op: str
) -> None:
    sync, aio = clients(provider)
    provider.script(Behavior(chunks=20, chunk_ms=5), Behavior(chunks=20, chunk_ms=5))
    with OPS[op](sync, stream=True) as stream:
        next(iter(stream))
    closed = OPS[op](sync, stream=True)
    next(iter(closed))
    closed.close()

    async def go() -> None:
        async with await OPS[op](aio, stream=True) as astream:
            await astream.__anext__()

    provider.script(Behavior(chunks=20, chunk_ms=5))
    asyncio.run(go())
    assert cancelled_count() == 3


def test_a_finished_stream_is_not_cancelled_by_close(
    measured: None, provider: FakeProvider
) -> None:
    sync, _ = clients(provider)
    with chat(sync, stream=True) as stream:
        list(stream)
    [call] = calls()
    assert call.status == "ok"


def test_a_dropped_stream_is_cancelled_and_freed(measured: None, provider: FakeProvider) -> None:
    sync, _ = clients(provider)
    provider.script(Behavior(chunks=20, chunk_ms=5))
    stream = chat(sync, stream=True)
    next(iter(stream))
    ref = weakref.ref(stream)
    del stream
    gc.collect()
    assert ref() is None  # our finalizer must not keep the stream alive
    assert _runtime.state is not None
    assert len(_runtime._abandoned) == 1
    chat(sync)  # the next call records the abandoned one
    assert cancelled_count() == 1
    assert not _runtime._abandoned


def test_cancelling_an_async_task_mid_stream(measured: None, provider: FakeProvider) -> None:
    _, aio = clients(provider)
    provider.script(Behavior(chunks=50, chunk_ms=20))

    async def go() -> None:
        stream = await chat(aio, stream=True)
        started = asyncio.Event()

        async def consume() -> None:
            async for _ in stream:
                started.set()

        task = asyncio.create_task(consume())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert cancelled_count() == 1


# --- Transparency (P3) --------------------------------------------------------------------


def test_results_and_signatures_are_unchanged(measured: None, provider: FakeProvider) -> None:
    sync, _ = clients(provider)
    from openai.resources.chat.completions import AsyncCompletions, Completions

    assert isinstance(chat(sync), openai.types.chat.ChatCompletion)
    classes: list[Any] = [Completions, AsyncCompletions]
    if "responses" in OPS:
        from openai.resources.responses import AsyncResponses, Responses

        assert isinstance(responses(sync), openai.types.responses.Response)
        classes += [Responses, AsyncResponses]
    for cls in classes:
        patched: Any = cls.create
        original = patched._gaslit_original
        assert patched.__name__ == "create"
        assert inspect.signature(patched) == inspect.signature(original)
        assert inspect.iscoroutinefunction(patched) == inspect.iscoroutinefunction(original)


def test_install_twice_wraps_once_and_uninstall_restores(measured: None) -> None:
    from openai.resources.chat.completions import Completions

    wrapped = Completions.create
    _openai.install()
    assert Completions.create is wrapped
    assert not hasattr(wrapped._gaslit_original, "_gaslit_original")  # type: ignore[attr-defined]
    _openai.uninstall()
    assert not hasattr(Completions.create, "_gaslit_original")


def test_raw_response_calls_are_counted_without_usage(
    measured: None, provider: FakeProvider
) -> None:
    sync, _ = clients(provider)
    raw = sync.chat.completions.with_raw_response.create(
        model="gpt-test", messages=[{"role": "user", "content": PROMPT}]
    )
    assert raw.parse().choices  # the user's call still works
    [call] = calls()
    assert (call.status, call.input_tokens, call.output_tokens) == ("ok", None, None)


def test_instrument_choices(provider: FakeProvider) -> None:
    from openai.resources.chat.completions import Completions

    def patched() -> bool:
        return hasattr(Completions.create, "_gaslit_original")

    gaslit.init(api_key=KEY, instrument=[])
    assert not patched()
    _runtime.reset()
    gaslit.init(api_key=KEY, instrument=["openai"])
    assert patched()


def test_kill_switch_means_no_patching(monkeypatch: pytest.MonkeyPatch) -> None:
    from openai.resources.chat.completions import Completions

    monkeypatch.setenv("GASLIT_ENABLED", "false")
    gaslit.init(api_key=KEY)
    assert not hasattr(Completions.create, "_gaslit_original")


# --- Hosts and routes ---------------------------------------------------------------------


def test_azure_reports_the_suffix_and_hides_the_deployment_name() -> None:
    client = SimpleNamespace(base_url="https://sentinelcorp.openai.azure.com/openai/v1/")
    gaslit.init(api_key=KEY)
    call = _openai._begin(
        SimpleNamespace(_client=client), "chat.completions", {"model": "acme-prod"}
    )
    assert call is not None
    assert (call.provider, call.route, call.endpoint_host) == (
        "openai",
        "azure",
        "openai.azure.com",
    )
    assert call.model_requested == "azure-deployment"
    assert "sentinelcorp" not in repr(call) and "acme-prod" not in repr(call)


@pytest.mark.parametrize(
    ("url", "provider", "route", "host"),
    [
        ("https://api.openai.com/v1/", "openai", "direct", "api.openai.com"),
        ("https://api.groq.com/openai/v1", "groq", "openai_compatible", "api.groq.com"),
        ("https://my-gateway.example.com/v1", "custom", "custom", None),
    ],
)
def test_other_hosts(url: str, provider: str, route: str, host: str | None) -> None:
    gaslit.init(api_key=KEY)
    resource = SimpleNamespace(_client=SimpleNamespace(base_url=url))
    call = _openai._begin(resource, "responses", {"model": "m", "stream": True})
    assert call is not None and call.streaming
    assert (call.provider, call.route, call.endpoint_host) == (provider, route, host)


# --- Fail open (P4) -----------------------------------------------------------------------


# (what to break, when the call path reaches it as (operation, streaming))
FAULTS = [
    ("gaslit._runtime.record", lambda op, stream: True),
    ("gaslit._model.Call.finish", lambda op, stream: True),
    ("gaslit._openai.detect", lambda op, stream: True),
    ("gaslit._openai._read_result", lambda op, stream: not stream or op == "responses"),
    ("gaslit._openai._read_usage", lambda op, stream: not stream or op == "responses"),
    ("gaslit._openai._chat_item", lambda op, stream: stream and op == "chat.completions"),
    ("gaslit._openai._event_item", lambda op, stream: stream and op == "responses"),
    ("gaslit._model.Call.first_token", lambda op, stream: stream),
]


@pytest.mark.parametrize("fault", FAULTS, ids=lambda f: f[0])
@MATRIX
def test_sdk_faults_never_reach_the_user(
    measured: None,
    provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
    fault: tuple[str, Callable[[str, bool], bool]],
    op: str,
    is_async: bool,
    stream: bool,
) -> None:
    sync, aio = clients(provider)
    client = aio if is_async else sync
    expected = run(op, client, is_async, stream)
    hits = []

    def boom(*_: object, **__: object) -> Any:
        hits.append(1)
        raise RuntimeError("SDK bug")

    monkeypatch.setattr(fault[0], boom)
    got = run(op, client, is_async, stream)
    assert bool(hits) == fault[1](op, stream)  # the fault really was reached
    assert type(got) is type(expected)
    if stream:
        assert [type(e) for e in got] == [type(e) for e in expected]
    else:
        assert got.model_dump(exclude={"id", "created", "created_at"}) == expected.model_dump(
            exclude={"id", "created", "created_at"}
        )


def test_calls_after_shutdown_pass_straight_through(provider: FakeProvider) -> None:
    gaslit.init(api_key=KEY)
    gaslit.shutdown()
    sync, _ = clients(provider)
    assert chat(sync).choices
    assert _runtime.state is None
