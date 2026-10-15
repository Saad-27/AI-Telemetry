"""Measuring calls Gaslit can't see on its own, with ``gaslit.track()``.

Run from sdk/python:  uv run python ../../examples/manual_track.py

Use it for raw HTTP calls, in-house model servers or unsupported libraries. Like the
automatic integrations, it records only timings, token counts and outcomes.
"""

import time
from collections.abc import Iterator

import gaslit

gaslit.init(debug=True, service="track-demo", sample_rate=1.0)


ANSWER = "here is a streamed answer from the in-house model"


def call_my_model(prompt: str) -> Iterator[str]:
    """A stand-in for your own model server: waits, then streams words."""
    time.sleep(0.2)
    for word in ANSWER.split():
        time.sleep(0.01)
        yield word


# A streaming call: time to first token, duration and token counts.
with gaslit.track("custom", "my-finetune", label="internal-llm", streaming=True) as t:
    words = []
    for chunk in call_my_model("Tell me something."):
        t.first_token()  # cheap; only the first call counts
        words.append(chunk)
    t.set_usage(input_tokens=1234, output_tokens=len(words))
print("streamed:", " ".join(words))

# A failure that doesn't raise, such as an HTTP response you check yourself.
with gaslit.track("custom", "my-finetune", label="internal-llm") as t:
    status = 503  # e.g. requests.post(...).status_code
    if status >= 400:
        t.set_error(http_status=status)
print("failed with HTTP", status)

# An exception inside the block is recorded by type (not its message) and re-raised unchanged.
try:
    with gaslit.track("local", "llama3.1:8b"):  # e.g. Ollama on this machine
        raise TimeoutError("the model took too long")
except TimeoutError:
    print("timed out")

gaslit.flush()
