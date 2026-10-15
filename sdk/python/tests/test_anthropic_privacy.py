"""Privacy sentinel tests for the Anthropic integration (brief §5 P1/P2, §6.9).

Marker strings go everywhere content could leak from: system prompts, messages, tool
definitions, metadata, headers, query strings, the provider key, model output, provider error
messages, and the customer's cloud names (a Foundry resource and deployment, an AWS account and
Bedrock profile). None may appear in any payload, debug output or log line.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import anthropic
import pytest
from conftest import KEY
from fake_ingest import FakeIngest
from fake_provider import Behavior, FakeProvider

import gaslit
from gaslit import _anthropic, _runtime

pytestmark = pytest.mark.sender

PROMPT = "SENTINEL_PROMPT_7f3a"
OUTPUT = "SENTINEL_OUTPUT_91c2"
PROVIDER_KEY = "sk-ant-SENTINEL-KEY"
SENTINELS = (
    PROMPT, OUTPUT, PROVIDER_KEY, "SENTINEL_SYSTEM", "SENTINEL_TOOL", "SENTINEL_USER",
    "SENTINEL_HDR", "SENTINEL_QS", "sentinelcorp", "sentineldeploy", "987650001234",
    "sentinelprofile",
)  # fmt: skip
TOOL = {
    "name": "SENTINEL_TOOL",
    "description": PROMPT,
    "input_schema": {"type": "object", "properties": {"SENTINEL_TOOL": {"type": "string"}}},
}


def test_no_sentinel_in_payloads_debug_output_or_logs(
    ingest: FakeIngest,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="gaslit")
    monkeypatch.setenv("ANTHROPIC_API_KEY", PROVIDER_KEY)
    gaslit.init(api_key=KEY, endpoint=ingest.url, debug=True, sample_rate=1.0)
    common: dict[str, Any] = {
        "model": "claude-test",
        "max_tokens": 10,
        "system": "SENTINEL_SYSTEM",
        "messages": [{"role": "user", "content": f"{PROMPT} {PROVIDER_KEY}"}],
        "tools": [TOOL],
        "metadata": {"user_id": "SENTINEL_USER"},
        "extra_headers": {"X-Sentinel": "SENTINEL_HDR"},
        "extra_query": {"key": "SENTINEL_QS"},
    }
    with FakeProvider(default=Behavior(word=f"{OUTPUT}_")) as provider:
        opts: dict[str, Any] = {"base_url": provider.url, "max_retries": 0}
        sync, aio = anthropic.Anthropic(**opts), anthropic.AsyncAnthropic(**opts)

        async def many() -> None:
            await aio.messages.create(**common)
            _ = [e async for e in await aio.messages.create(stream=True, **common)]
            async with aio.messages.stream(**common) as s:
                await s.get_final_text()

        sync.messages.create(**common)
        list(sync.messages.create(stream=True, **common))
        with sync.messages.stream(**common) as s:
            assert OUTPUT in s.get_final_text()
        asyncio.run(many())
        # Provider errors can echo the request. Only the status and short type may be kept.
        for status in (429, 529):
            provider.script(Behavior(status=status, echo=1))
            with pytest.raises(anthropic.APIStatusError) as caught:
                sync.messages.create(**common)
            assert PROMPT in str(caught.value)  # the user's exception still carries the message
        provider.script(Behavior(stream_error=529))
        with pytest.raises(anthropic.APIStatusError):
            list(sync.messages.create(stream=True, **common))

    # The customer's own names in cloud endpoints and model ids.
    for url, model in (
        ("https://sentinelcorp.services.ai.azure.com/anthropic/", "sentineldeploy"),
        ("https://bedrock-runtime.us-east-1.amazonaws.com",
         "arn:aws:bedrock:us-east-1:987650001234:application-inference-profile/sentinelprofile"),
        ("https://bedrock-runtime.us-east-1.amazonaws.com",
         "arn:aws:bedrock:us-east-1:987650001234:inference-profile/us.anthropic.claude-x"),
    ):  # fmt: skip
        resource = SimpleNamespace(_client=SimpleNamespace(base_url=url))
        call = _anthropic._begin(resource, "messages", {"model": model})
        assert call is not None
        call.finish()
        _runtime.record(call)

    gaslit.flush()
    requests = ingest.wait_for(1)
    sent = [e for r in requests if (e := r.envelope) is not None]
    samples = [s for e in sent for s in e["samples"]]
    assert len(samples) >= 9  # the calls really were sent
    assert {s["route"] for s in samples} >= {"azure", "bedrock"}
    out, err = capfd.readouterr()
    assert "[gaslit] payload " in err
    for text in (*(r.body.decode() for r in requests), out, err, caplog.text):
        for marker in (*SENTINELS, KEY):
            assert marker not in text
    for r in requests:  # the ingest key goes only in the Authorization header
        assert PROVIDER_KEY not in str({k: v for k, v in r.headers.items() if k != "Authorization"})
