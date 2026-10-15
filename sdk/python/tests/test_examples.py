"""The examples (examples/) run and print the events they should (M2 exit criterion).

Each example runs in its own process, against the fake provider, in debug mode with no key,
and its printed payload is checked. The examples use the latest client libraries, so the
older-version CI jobs don't run this file.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

EXAMPLES = Path(__file__).resolve().parents[3] / "examples"
PREFIX = "[gaslit] payload "


def run(name: str) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Run an example. Returns its payload text, summaries and samples."""
    done = subprocess.run(
        [sys.executable, str(EXAMPLES / name)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    lines = [line[len(PREFIX) :] for line in done.stderr.splitlines() if line.startswith(PREFIX)]
    assert lines, done.stderr
    payloads = [json.loads(line) for line in lines]
    summaries = [s for p in payloads for s in p["summaries"]]
    samples = [s for p in payloads for s in p["samples"]]
    return "\n".join(lines), summaries, samples


def totals(summaries: list[dict[str, Any]], **match: object) -> dict[str, Any]:
    """Counts summed over the matching series (a run can cross a minute boundary)."""
    found = [s for s in summaries if match.items() <= s["series"].items()]
    assert found, (match, summaries)
    keys = ("count", "ok", "output_tokens_sum", "output_tokens_estimated_count")
    sums: dict[str, Any] = {k: sum(s[k] for s in found) for k in keys}
    sums["errors"] = {}
    for s in found:
        for name, n in s["errors"].items():
            sums["errors"][name] = sums["errors"].get(name, 0) + n
    return sums


def sample(samples: list[dict[str, Any]], **match: object) -> dict[str, Any]:
    found = [s for s in samples if match.items() <= s.items()]
    assert len(found) == 1, (match, samples)
    return found[0]


def test_openai_chat() -> None:
    text, summaries, samples = run("openai_chat.py")
    where = {"provider": "openai", "route": "direct", "endpoint_host": "api.openai.com"}
    plain = totals(summaries, **where, model="gpt-4o-mini", streaming=False, service="chat-demo")
    assert (plain["count"], plain["ok"], plain["errors"]) == (2, 1, {"rate_limited": 1})
    streamed = totals(summaries, **where, streaming=True)
    assert (streamed["count"], streamed["ok"]) == (1, 1)
    # Without include_usage (which the SDK never adds, P3) output tokens are estimated.
    assert (streamed["output_tokens_sum"], streamed["output_tokens_estimated_count"]) == (8, 1)
    assert sample(samples, streaming=True)["ttft_ms"] >= 150
    assert sample(samples, status="error")["http_status"] == 429
    assert "colours" not in text and "word0" not in text  # prompt and reply (P1)


def test_anthropic_summariser() -> None:
    text, summaries, samples = run("anthropic_summariser.py")
    where = {"provider": "anthropic", "route": "direct", "model": "claude-sonnet-5-5"}
    for size in ("xs", "s", "l"):
        assert totals(summaries, **where, streaming=True, input_bucket=size)["ok"] == 1
    failed = sample(samples, status="error")
    assert (failed["http_status"], failed["error_class"]) == (529, "overloaded")
    longest = sample(samples, input_tokens=21_600)
    assert longest["ttft_ms"] >= 216  # the fake takes longer to start on a long input
    assert "quarterly" not in text and "Summarise" not in text  # document and system prompt


def test_cloud_hosts() -> None:
    text, _, samples = run("cloud_hosts.py")
    azure = sample(samples, route="azure")
    assert (azure["provider"], azure["endpoint_host"], azure["endpoint_region"]) == (
        "openai",
        "openai.azure.com",
        None,
    )
    assert (azure["model_requested"], azure["model_returned"]) == (
        "azure-deployment",
        "gpt-4o-2024-08-06",
    )
    bedrock = sample(samples, route="bedrock")
    assert (bedrock["provider"], bedrock["endpoint_region"], bedrock["streaming"]) == (
        "anthropic",
        "eu-central-1",
        False,
    )
    vertex = sample(samples, route="vertex")
    assert (vertex["provider"], vertex["endpoint_region"], vertex["streaming"]) == (
        "anthropic",
        "us-east5",
        True,
    )
    assert all(s["status"] == "ok" for s in samples)
    # The customer's Azure resource and deployment names and GCP project never appear (P2).
    assert "acme" not in text


def test_manual_track() -> None:
    text, _, samples = run("manual_track.py")
    streamed = sample(samples, streaming=True)
    assert (streamed["provider"], streamed["model_requested"], streamed["label"]) == (
        "custom",
        "my-finetune",
        "internal-llm",
    )
    assert streamed["ttft_ms"] >= 200 and streamed["status"] == "ok"
    assert (streamed["input_tokens"], streamed["output_tokens"]) == (1234, 9)
    failed = sample(samples, http_status=503)
    assert failed["error_class"] == "server_error"
    timed_out = sample(samples, provider="local")
    assert (timed_out["route"], timed_out["error_class"]) == ("local", "timeout")
    assert "too long" not in text  # exception messages are never read (P1)


@pytest.mark.parametrize("name", sorted(p.name for p in EXAMPLES.glob("[!_]*.py")))
def test_every_example_is_tested(name: str) -> None:
    assert f"test_{name.removesuffix('.py')}" in globals()
