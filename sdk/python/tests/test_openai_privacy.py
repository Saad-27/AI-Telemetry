"""Privacy sentinel tests for the OpenAI integration (brief §5 P1/P2, §6.9).

Marker strings go everywhere content could leak from: prompts, tool definitions, end-user ids,
metadata, headers, query strings, the provider key, model output and provider error messages.
None may appear in any payload, debug output or log line.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from types import SimpleNamespace
from typing import Any

import openai
import pytest
from conftest import KEY
from fake_ingest import FakeIngest
from fake_provider import Behavior, FakeProvider

import gaslit
from gaslit import _openai, _runtime

pytestmark = pytest.mark.sender
has_responses = importlib.util.find_spec("openai.resources.responses") is not None

PROMPT = "SENTINEL_PROMPT_7f3a"
OUTPUT = "SENTINEL_OUTPUT_91c2"
PROVIDER_KEY = "sk-SENTINEL-KEY"
SENTINELS = (
    PROMPT, OUTPUT, PROVIDER_KEY, "sentinelcorp", "SENTINEL_QS", "SENTINEL_TOOL", "SENTINEL_USER",
    "SENTINEL_HDR", "SENTINEL_META", "SENTINEL_DEPLOYMENT",
)  # fmt: skip
TOOL = {
    "type": "function",
    "function": {
        "name": "SENTINEL_TOOL",
        "description": PROMPT,
        "parameters": {"type": "object", "properties": {"SENTINEL_TOOL": {"type": "string"}}},
    },
}


def test_no_sentinel_in_payloads_debug_output_or_logs(
    ingest: FakeIngest,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="gaslit")
    monkeypatch.setenv("OPENAI_API_KEY", PROVIDER_KEY)
    gaslit.init(api_key=KEY, endpoint=ingest.url, debug=True, sample_rate=1.0)
    messages: Any = [
        {"role": "system", "content": PROMPT},
        {"role": "user", "content": f"{PROMPT} {PROVIDER_KEY}"},
    ]
    common: dict[str, Any] = {
        "model": "gpt-test",
        "tools": [TOOL],
        "user": "SENTINEL_USER",
        "extra_headers": {"X-Sentinel": "SENTINEL_HDR"},
        "extra_query": {"key": "SENTINEL_QS"},
        "extra_body": {"metadata": {"k": "SENTINEL_META"}},
    }
    with FakeProvider(default=Behavior(word=f"{OUTPUT}_")) as provider:
        opts: dict[str, Any] = {
            "base_url": provider.url + "/v1",
            "api_key": PROVIDER_KEY,
            "max_retries": 0,
        }
        sync, aio = openai.OpenAI(**opts), openai.AsyncOpenAI(**opts)

        async def many() -> None:
            for stream in (False, True):
                result: Any = await aio.chat.completions.create(
                    messages=messages, stream=stream, **common
                )
                _ = [c async for c in result] if stream else result

        for stream in (False, True):
            r1 = sync.chat.completions.create(messages=messages, stream=stream, **common)
            r2 = (
                sync.responses.create(input=messages, stream=stream, **common)
                if has_responses
                else r1
            )
            if stream:
                list(r1)
                list(r2)
        asyncio.run(many())
        # Provider errors can echo the request. Only the status and short code may be kept.
        for status in (429, 503):
            provider.script(Behavior(status=status, echo=1))
            with pytest.raises(openai.APIStatusError) as caught:
                sync.chat.completions.create(messages=messages, **common)
            assert PROMPT in str(caught.value)  # the user's exception still carries the message

    # An Azure client: the resource name and the deployment name are the customer's.
    azure = SimpleNamespace(
        _client=SimpleNamespace(base_url="https://sentinelcorp.openai.azure.com/openai/v1/")
    )
    call = _openai._begin(azure, "chat.completions", {"model": "SENTINEL_DEPLOYMENT"})
    assert call is not None
    call.finish()
    _runtime.record(call)

    gaslit.flush()
    requests = ingest.wait_for(1)
    sent = [e for r in requests if (e := r.envelope) is not None]
    assert sum(len(e["samples"]) for e in sent) >= 5  # the calls really were sent
    assert any(s["route"] == "azure" for e in sent for s in e["samples"])
    out, err = capfd.readouterr()
    assert "[gaslit] payload " in err
    for text in (*(r.body.decode() for r in requests), out, err, caplog.text):
        for marker in (*SENTINELS, KEY):
            assert marker not in text
    for r in requests:  # the ingest key goes only in the Authorization header
        assert PROVIDER_KEY not in str({k: v for k, v in r.headers.items() if k != "Authorization"})
