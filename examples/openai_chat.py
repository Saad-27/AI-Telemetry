"""OpenAI chat completions, streaming and not, measured by Gaslit.

Run from sdk/python:  uv run python ../../examples/openai_chat.py

Gaslit prints the payload it would send to stderr when you call ``flush()``.
"""

import gaslit
import openai
from _demo import Behavior, offline

# No ingest key and debug=True: payloads are printed, never sent. sample_rate=1.0 prints every
# call individually as well as in the per-minute summary; the default sends 5% of successes.
gaslit.init(debug=True, service="chat-demo", environment="dev", sample_rate=1.0)

with offline() as fake:
    # http:// so the request goes to the local fake provider (see _demo.py). In a real app,
    # leave base_url out.
    client = openai.OpenAI(base_url="http://api.openai.com/v1", api_key="sk-fake", max_retries=0)
    messages = [{"role": "user", "content": "Name three colours."}]

    reply = client.chat.completions.create(model="gpt-4o-mini", messages=messages)
    print("non-streaming:", reply.choices[0].message.content)

    fake.script(Behavior(latency_ms=150, chunk_ms=20, chunks=8))  # a slower, longer reply
    stream = client.chat.completions.create(model="gpt-4o-mini", messages=messages, stream=True)
    print("streaming:", "".join(c.choices[0].delta.content or "" for c in stream if c.choices))

    fake.script(Behavior(status=429, retry_after_s=2))
    try:
        client.chat.completions.create(model="gpt-4o-mini", messages=messages)
    except openai.RateLimitError as e:
        print("rate limited:", e.status_code)

gaslit.flush()  # short scripts flush before exiting; servers send in the background
