"""Privacy sentinel tests for ``track()`` (brief §5 P1/P2, §6.9).

Marker strings are put everywhere content could leak from. None may appear in any payload,
debug output or log line. The ingest key may only appear in the Authorization header.
"""

import logging

import pytest
from conftest import KEY
from fake_ingest import FakeIngest

import yourpkg

pytestmark = pytest.mark.sender

PROMPT = "SENTINEL_PROMPT_7f3a"
OUTPUT = "SENTINEL_OUTPUT_91c2"
PROVIDER_KEY = "sk-SENTINEL-KEY"
SENTINELS = (PROMPT, OUTPUT, PROVIDER_KEY, "sentinelcorp", "SENTINEL_QS", "SENTINEL_ERR")


def fake_model(prompt: str) -> list[str]:
    """Stands in for a model the SDK can't see into."""
    return [OUTPUT, prompt]


def run_tracked_calls() -> None:
    with yourpkg.track(provider="custom", model="m", streaming=True) as t:
        for _ in fake_model(f"system: {PROMPT}"):
            t.first_token()
        t.set_usage(input_tokens=10, output_tokens=20)
    with yourpkg.track(provider="openai", model="gpt-x", route="direct") as t:
        t.set_error(http_status=429, code=f"rate_limit {OUTPUT}")  # invalid code: dropped
    with yourpkg.track(provider="openai", model="gpt-x", route="direct") as t:
        t.set_error(http_status=400, code="invalid_request_error")
    with pytest.raises(RuntimeError), yourpkg.track(provider="anthropic", model="m"):
        raise RuntimeError(f"SENTINEL_ERR: {PROMPT} {PROVIDER_KEY}")
    # Content passed where metadata belongs is normalised, never echoed. (A valid tag passed
    # as a label is the user's own metadata and is sent, so these use content-shaped text.)
    text = f"{PROMPT} {OUTPUT}"
    with yourpkg.track(provider=text, model=text, label=text, route=text):
        pass


def test_no_sentinel_in_payloads_debug_output_or_logs(
    ingest: FakeIngest,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="yourpkg")
    monkeypatch.setenv("OPENAI_API_KEY", PROVIDER_KEY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", PROVIDER_KEY)
    yourpkg.init(api_key=KEY, endpoint=ingest.url, debug=True, sample_rate=1.0)
    run_tracked_calls()
    yourpkg.flush()

    [req] = ingest.wait_for(1)
    assert req.status == 202
    env = req.envelope
    assert env is not None and len(env["samples"]) >= 4  # the calls really were sent
    out, err = capfd.readouterr()
    assert "[yourpkg] payload " in err
    headers = {k: v for k, v in req.headers.items() if k != "Authorization"}
    for text in (req.body.decode(), str(headers), out, err, caplog.text):
        for marker in (*SENTINELS, KEY):
            assert marker not in text


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://ingest.example.com/?key=SENTINEL_QS",
        "https://user:SENTINEL_QS@ingest.example.com",
        "http://sentinelcorp.openai.azure.com/SENTINEL_QS",
        "https://ingest.example.com:SENTINEL_QS",
    ],
)
@pytest.mark.parametrize("key", [PROVIDER_KEY, f"rm_live_{PROMPT} x"])
def test_bad_config_is_never_echoed(
    endpoint: str, key: str, capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="yourpkg")
    yourpkg.init(api_key=key, endpoint=endpoint, debug=True)
    with yourpkg.track(provider="custom", model="m"):
        pass
    yourpkg.flush()
    out, err = capfd.readouterr()
    assert "The ingest key is malformed" in caplog.text
    for text in (out, err, caplog.text):
        for marker in SENTINELS:
            assert marker not in text
