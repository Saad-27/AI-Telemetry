# gaslit (Python SDK)

Passive reliability metrics for the AI model calls your app already makes.
Metadata only: no prompts, outputs or keys ever leave your process.

Status: pre-release (milestone M2). Calls made with the `openai` library (1.30+:
`chat.completions.create` and `responses.create`, sync and async, streaming or not) are measured
automatically after `init()`, including Azure and OpenAI-compatible endpoints. `anthropic`
support is next. Anything else can be measured by hand with `track()`.

```python
import gaslit

gaslit.init(service="summariser", environment="prod")  # key from GASLIT_KEY

with gaslit.track(provider="custom", model="my-finetune", streaming=True) as t:
    for chunk in call_my_model(...):
        t.first_token()
    t.set_usage(input_tokens=1234, output_tokens=210)
```

- **Audit what is sent:** `init(debug=True)` (or `GASLIT_DEBUG=1`) prints every payload to
  stderr exactly as sent. Without a key it prints and sends nothing.
- **Serverless and short scripts:** call `gaslit.flush()` at the end of each handler. Background
  threads may be frozen between invocations. Data is also flushed at normal interpreter exit.
- **Choose libraries:** `init(instrument=["openai"])`. The default is every supported library
  that is installed; `instrument=[]` measures only `track()` blocks.
- **Turn it off:** `GASLIT_ENABLED=false`. Nothing is patched.

## Local development

```sh
uv run python tests/fake_ingest.py 8765          # a local stand-in for the ingest API
GASLIT_ENDPOINT=http://127.0.0.1:8765 GASLIT_KEY=gl_live_dev python your_app.py
uv run python tests/bench_overhead.py            # call-path overhead vs the budget
```

Licence: Apache-2.0.
