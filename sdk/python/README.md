# yourpkg (Python SDK)

Passive reliability metrics for the AI model calls your app already makes.
Metadata only: no prompts, outputs or keys ever leave your process.

Status: pre-release (milestone M1). Manual instrumentation with `track()` works; automatic
`openai` and `anthropic` instrumentation arrives in M2.

```python
import yourpkg

yourpkg.init(service="summariser", environment="prod")  # key from YOURPKG_KEY

with yourpkg.track(provider="custom", model="my-finetune", streaming=True) as t:
    for chunk in call_my_model(...):
        t.first_token()
    t.set_usage(input_tokens=1234, output_tokens=210)
```

- **Audit what is sent:** `init(debug=True)` (or `YOURPKG_DEBUG=1`) prints every payload to
  stderr exactly as sent. Without a key it prints and sends nothing.
- **Serverless and short scripts:** call `yourpkg.flush()` at the end of each handler. Background
  threads may be frozen between invocations. Data is also flushed at normal interpreter exit.
- **Turn it off:** `YOURPKG_ENABLED=false`.

## Local development

```sh
uv run python tests/fake_ingest.py 8765          # a local stand-in for the ingest API
YOURPKG_ENDPOINT=http://127.0.0.1:8765 YOURPKG_KEY=rm_live_dev python your_app.py
uv run python tests/bench_overhead.py            # call-path overhead vs the budget
```

Licence: Apache-2.0.
