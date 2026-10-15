"""The fake provider (tools/fake-provider) speaks the wire formats the M2 tests depend on.

These tests use plain HTTP, not the official client libraries, so the SDK keeps zero
dependencies; the shapes were also checked against the real ``openai`` and ``anthropic``
clients when the server was written.
"""

import json
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any
from unittest.mock import ANY

import pytest
from fake_provider import Behavior, FakeProvider

BODIES: dict[str, dict[str, Any]] = {
    "/v1/chat/completions": {"model": "m", "messages": []},
    "/v1/responses": {"model": "m", "input": "x"},
    "/v1/messages": {"model": "m", "max_tokens": 5, "messages": []},
}


@pytest.fixture
def provider() -> Iterator[FakeProvider]:
    with FakeProvider() as fake:
        yield fake


def post(
    fake: FakeProvider,
    path: str,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> tuple[int, dict[str, str], bytes]:
    body = json.dumps({**BODIES[path], **extra}).encode()
    req = urllib.request.Request(fake.url + path, body, headers or {}, method="POST")  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def events(raw: bytes) -> list[dict[str, Any]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in raw.decode().splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


def test_behavior_parse() -> None:
    assert Behavior.parse("status=429, latency_ms=200") == Behavior(status=429, latency_ms=200)
    with pytest.raises(ValueError, match="unknown behavior"):
        Behavior.parse("stauts=429")


@pytest.mark.parametrize("path", BODIES)
def test_non_streaming_reports_usage(provider: FakeProvider, path: str) -> None:
    status, _, raw = post(provider, path)
    usage = json.loads(raw)["usage"]
    assert status == 200
    assert 5 in usage.values()  # output tokens
    assert provider.calls[0].model == "m"


def test_chat_stream_ends_with_usage_and_done_only_when_asked(provider: FakeProvider) -> None:
    _, _, raw = post(provider, "/v1/chat/completions", stream=True)
    assert raw.endswith(b"data: [DONE]\n\n")
    assert all("usage" not in e for e in events(raw))
    _, _, raw = post(
        provider, "/v1/chat/completions", stream=True, stream_options={"include_usage": True}
    )
    assert events(raw)[-1]["usage"]["completion_tokens"] == 5


def test_responses_stream(provider: FakeProvider) -> None:
    _, _, raw = post(provider, "/v1/responses", stream=True)
    kinds = [e["type"] for e in events(raw)]
    assert kinds[0] == "response.created"
    assert kinds[-1] == "response.completed"
    assert kinds.count("response.output_text.delta") == 5
    assert events(raw)[-1]["response"]["usage"]["output_tokens"] == 5


def test_messages_stream(provider: FakeProvider) -> None:
    _, _, raw = post(provider, "/v1/messages", stream=True)
    kinds = [e["type"] for e in events(raw)]
    assert kinds[0] == "message_start"
    assert kinds[-1] == "message_stop"
    assert events(raw)[-2]["usage"]["output_tokens"] == 5


def test_messages_stream_error_event_and_cache_counts(provider: FakeProvider) -> None:
    provider.script(Behavior(stream_error=529, cached_tokens=7))
    status, _, raw = post(provider, "/v1/messages", stream=True)
    start, *_, last = events(raw)
    assert status == 200  # Anthropic reports a mid-stream failure as an event
    assert start["message"]["usage"]["cache_read_input_tokens"] == 7
    assert b"event: error\n" in raw
    assert last == {"type": "error", "error": {"type": "overloaded_error", "message": ANY}}


@pytest.mark.parametrize(
    ("status", "retry_after"), [(429, "2"), (529, "0"), (500, "0"), (503, "0")]
)
def test_errors_use_each_providers_shape(
    provider: FakeProvider, status: int, retry_after: str
) -> None:
    behavior = f"status={status},retry_after_s={retry_after}"
    got, headers, raw = post(provider, "/v1/messages", {"x-fake-behavior": behavior})
    assert got == status
    assert json.loads(raw)["type"] == "error"
    assert headers.get("Retry-After") == (retry_after if retry_after != "0" else None)
    _, _, raw = post(provider, "/v1/chat/completions", {"x-fake-behavior": behavior})
    assert "message" in json.loads(raw)["error"]


def test_error_status_wins_over_streaming(provider: FakeProvider) -> None:
    provider.script(429)
    status, headers, _ = post(provider, "/v1/chat/completions", stream=True)
    assert status == 429
    assert headers["Content-Type"] == "application/json"


def test_script_then_default(provider: FakeProvider) -> None:
    provider.script(529, Behavior(chunks=2))
    statuses = [post(provider, "/v1/messages")[0] for _ in range(3)]
    assert statuses == [529, 200, 200]
    assert [c.status for c in provider.wait_for(3)] == statuses


def test_slow_stream_waits_for_first_token_and_between_tokens(provider: FakeProvider) -> None:
    provider.script(Behavior(latency_ms=150, chunk_ms=50, chunks=3))
    start = time.monotonic()
    post(provider, "/v1/messages", stream=True)
    assert time.monotonic() - start >= 0.3  # 150 + 3 * 50


def test_timeout_is_a_long_latency(provider: FakeProvider) -> None:
    provider.script(Behavior(latency_ms=1000))
    req = urllib.request.Request(  # noqa: S310
        provider.url + "/v1/messages", json.dumps(BODIES["/v1/messages"]).encode(), method="POST"
    )
    with pytest.raises(TimeoutError):
        urllib.request.urlopen(req, timeout=0.2)  # noqa: S310


def test_bad_behavior_header_and_unknown_route(provider: FakeProvider) -> None:
    assert post(provider, "/v1/messages", {"x-fake-behavior": "nope=1"})[0] == 400
    req = urllib.request.Request(provider.url + "/v2/other", b"{}", method="POST")  # noqa: S310
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=5)  # noqa: S310
    assert err.value.code == 404
