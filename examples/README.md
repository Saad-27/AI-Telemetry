# Examples

Small apps that make AI model calls with Gaslit switched on. Each one runs offline against
the fake provider (`tools/fake-provider/`), so no keys or tokens are needed, and prints the
payload Gaslit would send.

| Example | Shows |
|---|---|
| [openai_chat.py](openai_chat.py) | OpenAI chat completions: non-streaming, streaming, and a 429 |
| [anthropic_summariser.py](anthropic_summariser.py) | `messages.stream()` on short to long inputs (input size buckets), and a 529 |
| [cloud_hosts.py](cloud_hosts.py) | Azure OpenAI, Bedrock and Vertex: route, region, and the customer's names left out |
| [manual_track.py](manual_track.py) | `gaslit.track()` for an in-house model: a stream, a non-raising failure, an exception |

## Run

From `sdk/python`, where the dev environment has `openai` and `anthropic` installed:

```bash
uv run python ../../examples/openai_chat.py
```

The app's own output goes to stdout. The payload goes to stderr as one JSON line starting
with `[gaslit] payload`, exactly as it would be sent. To read it indented:

```bash
uv run python ../../examples/openai_chat.py 2>&1 >/dev/null \
  | sed -n 's/^\[gaslit\] payload //p' | python -m json.tool
```

Look for what is **not** there: no prompts, replies, keys, URL paths or error messages.

## How they stay offline

Each example calls `gaslit.init(debug=True)` with no ingest key, so payloads are printed and
never sent. `sample_rate=1.0` prints every call individually as well as in the per-minute
summary.

The clients use `http://` base URLs with the real provider hostnames, and `_demo.offline()`
sets `HTTP_PROXY` so those requests go to the local fake provider. Gaslit reads only the
hostname, so the events match what a real call would produce. In your own app, none of this
is needed: call `gaslit.init()` once at startup, and `gaslit.flush()` before a short script
exits.

## Send to the local server

With `make dev` running, `make dev-key` prints a key. Then, from `sdk/python`:

```bash
GASLIT_ENDPOINT=http://127.0.0.1:8000 GASLIT_KEY=gl_live_... \
  uv run python ../../examples/openai_chat.py
```

The payloads are still printed, and now also stored in the local database.

`sdk/python/tests/test_examples.py` runs every example and checks its payload.
