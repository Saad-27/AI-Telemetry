"""An Anthropic summariser with long inputs, measured by Gaslit.

Run from sdk/python:  uv run python ../../examples/anthropic_summariser.py

Long inputs are slow to start, so Gaslit groups calls by input size (``input_bucket``):
a 20k-token prompt's time to first token is never averaged with a 200-token one's.
"""

import anthropic
import gaslit
from _demo import Behavior, offline

gaslit.init(debug=True, service="summariser", environment="dev", sample_rate=1.0)

PARAGRAPH = "The quarterly report covers revenue, hiring, incidents and the roadmap. " * 20


def summarise(client: anthropic.Anthropic, document: str) -> str:
    with client.messages.stream(
        model="claude-sonnet-5-5",
        max_tokens=300,
        system="Summarise the document in three sentences.",
        messages=[{"role": "user", "content": document}],
    ) as stream:
        return "".join(stream.text_stream)


with offline() as fake:
    client = anthropic.Anthropic(
        base_url="http://api.anthropic.com", api_key="sk-ant-fake", max_retries=0
    )
    for pages in (2, 10, 60):  # one input in each size bucket
        document = PARAGRAPH * pages
        # The real API counts the input; the fake is told roughly 4 characters per token.
        # It also takes longer to start on a longer input, like the real thing.
        tokens = len(document) // 4
        fake.script(Behavior(input_tokens=tokens, latency_ms=tokens // 100, chunks=40))
        summary = summarise(client, document)
        print(f"{tokens:>6} input tokens -> {len(summary.split())}-word summary")

    fake.script(Behavior(status=529))
    try:
        summarise(client, PARAGRAPH * 10)
    except anthropic.APIStatusError as e:
        print("overloaded:", e.status_code)

gaslit.flush()
