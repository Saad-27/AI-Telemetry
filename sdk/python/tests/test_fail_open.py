"""Fail-open tests for ``track()`` (brief §5 P4, §6.9).

Whatever the ingest API or the SDK's internals do, the user's code returns the same result
or raises the same exception, is never slowed down by I/O, and no SDK exception escapes.
"""

import socket
import time
from collections.abc import Callable

import pytest
from conftest import KEY
from fake_ingest import GARBAGE, FakeIngest, Reply

import yourpkg
from yourpkg import _runtime

pytestmark = pytest.mark.sender

RESULT = object()
CALL_BUDGET_S = 0.05  # generous: a tracked call costs microseconds


class UserError(Exception):
    pass


def user_code_ok() -> object:
    with yourpkg.track(provider="custom", model="m", streaming=True) as t:
        t.first_token()
        t.set_usage(input_tokens=1, output_tokens=2)
        return RESULT


def user_code_raises(err: BaseException) -> None:
    with yourpkg.track(provider="custom", model="m"):
        raise err


def assert_transparent() -> None:
    """The user's call behaves exactly as without the SDK, and fast."""
    for _ in range(20):
        t0 = time.perf_counter()
        assert user_code_ok() is RESULT
        err = UserError("boom")
        with pytest.raises(UserError) as info:
            user_code_raises(err)
        assert info.value is err
        assert time.perf_counter() - t0 < CALL_BUDGET_S


def closed_port() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}"


@pytest.mark.parametrize(
    "reply",
    [
        Reply(500),
        Reply(401),
        Reply(429, {"Retry-After": "300"}),
        Reply(400),
        Reply(413),
        Reply(GARBAGE),
        Reply(202, delay=2.0),  # longer than the SDK's (shortened) timeout
    ],
    ids=["500", "401", "429", "400", "413", "garbage", "timeout"],
)
def test_ingest_failures(ingest: FakeIngest, reply: Reply) -> None:
    ingest.replies.extend([reply] * 50)
    yourpkg.init(api_key=KEY, endpoint=ingest.url, max_batch=1)
    assert_transparent()
    t0 = time.monotonic()
    yourpkg.flush(timeout=0.3)
    assert time.monotonic() - t0 < 1.0  # the flush cap holds even while the ingest misbehaves
    assert ingest.received  # the sender really talked to it
    assert_transparent()  # still fine after the failure has been seen


def test_ingest_unreachable() -> None:
    yourpkg.init(api_key=KEY, endpoint=closed_port())
    assert_transparent()
    yourpkg.flush(timeout=0.3)
    assert_transparent()


def test_unresolvable_ingest_host() -> None:
    yourpkg.init(api_key=KEY, endpoint="https://ingest.yourpkg.invalid")
    assert_transparent()
    yourpkg.flush(timeout=0.3)
    assert_transparent()


def boom(*_: object, **__: object) -> None:
    raise RuntimeError("internal fault")


FAULTS: dict[str, Callable[[pytest.MonkeyPatch], None]] = {
    "aggregator.add": lambda m: m.setattr("yourpkg._aggregator.Aggregator.add", boom),
    "queue.put": lambda m: m.setattr("yourpkg._queue.BoundedQueue.put", boom),
    "call.finish": lambda m: m.setattr("yourpkg._model.Call.finish", boom),
    "call.first_token": lambda m: m.setattr("yourpkg._model.Call.first_token", boom),
    "sender.start": lambda m: m.setattr("yourpkg._sender.Sender.start", boom),
    "sender.wake": lambda m: m.setattr("yourpkg._sender.Sender.wake", boom),
    "sender.flush": lambda m: m.setattr("yourpkg._sender.Sender.flush", boom),
    "record": lambda m: m.setattr("yourpkg._runtime.record", boom),
    "log.warn": lambda m: m.setattr("yourpkg._log.warn", boom),
}


@pytest.mark.parametrize("fault", FAULTS)
def test_internal_faults_on_the_call_path(
    ingest: FakeIngest, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    yourpkg.init(api_key=KEY, endpoint=ingest.url, max_batch=1)
    FAULTS[fault](monkeypatch)
    assert_transparent()
    yourpkg.flush(timeout=0.3)
    yourpkg.shutdown(timeout=0.3)


@pytest.mark.parametrize(
    "target",
    ["yourpkg._sender.Sender._post", "yourpkg._sender.json.dumps", "yourpkg._sender.sample_record"],
)
def test_sender_survives_internal_faults(
    ingest: FakeIngest, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    yourpkg.init(api_key=KEY, endpoint=ingest.url)
    with monkeypatch.context() as m:
        m.setattr(target, boom)
        assert_transparent()
        yourpkg.flush(timeout=0.5)
    state = _runtime.state
    assert state is not None and state.sender._thread is not None
    assert state.sender._thread.is_alive()
    with yourpkg.track(provider="custom", model="after"):
        pass
    yourpkg.flush()
    models = {s["series"]["model"] for e in ingest.accepted() for s in e["summaries"]}
    assert "after" in models  # the faulty batch was dropped; later ones still go out
