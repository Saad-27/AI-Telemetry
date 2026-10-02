# Project Brief: LLM Reliability Monitor (working name)

> **Owner:** Muhammad Saad
> **Written:** 1 October 2026, distilled from a long planning conversation
> **Status:** Pre-build. Scope is the **free tier only** for now.

---

## 0. Read this first (instructions for the AI agent)

- This document is the single source of truth for **what** is being built, **why**, and **how**. Read it fully before writing code or proposing architecture.
- **Build only the free tier** (Section 7). Paid features (Section 8) are parked. Do not build them, but do not design them out either. Section 8.2 lists the small hooks to leave in place.
- **Privacy invariants (Section 5) and security requirements (Section 11) are non-negotiable.** If a task or shortcut conflicts with them, stop and ask Saad.
- **Open decisions (Section 16):** ask Saad before choosing. Each one has a recommended default to use if he says "go with the default".
- **Facts to verify (Section 17):** SDK internals, header names, status-page formats and similar details change often. Check current docs before relying on anything listed there.
- **Placeholders** (replace once a name is chosen):

  | Placeholder | Meaning |
  |---|---|
  | `<Product>` | Product name |
  | `yourpkg` | Python package / import name |
  | `YOURPKG_KEY` | Environment variable holding the ingest key |
  | `rm_live_` | Ingest key prefix |
  | `<domain>` | Website domain (`app.<domain>`, `ingest.<domain>`) |

- **Working style:** Saad wants to understand and be able to explain every part of this system in job interviews.
  - Record every non-trivial technical choice as a short ADR in `docs/decisions/NNNN-title.md`, with context, options considered, decision and trade-offs.
  - Explain choices briefly in PR descriptions.
  - Keep chat responses concise and elaborate only when asked.
  - Prefer simple, well-understood designs over clever ones.
- **Efficiency is a core requirement, not a later optimisation.** Read Section 21 (no bloat) and Section 19 (what actually gets sent). Do not add components, dependencies or abstractions without a measured need (Section 20.3).
- Appendix C contains a short suggested `CLAUDE.md` that points back to this brief.

---

## 1. Summary

<Product> is an **open-source SDK plus a hosted dashboard**. It tells developers whether the AI models their app depends on are healthy, slow or failing, **measured from the app's own real traffic**.

**How the developer sets it up**

- They install a small package and add two lines of code.
- The package wraps the AI model calls the app **already makes**.

**What the package records**

- It records **metadata only**: which model, which provider and route, timings such as time to first token, token counts and the error type.
- It **never** captures prompts, outputs or API keys.
- It **never** makes extra calls, so the user pays nothing extra to their AI provider.
- Batches of metadata are sent to our ingest API using a **write-only project key**.

**What the website shows**

- A live feed of calls and health per model.
- Official provider incidents overlaid on the charts.
- A comparison of the user's numbers against an **anonymised baseline built from all users** ("you vs everyone").

Together these answer the core question: **"Is it me, or is it the model provider?"**

---

## 2. Why this project exists

### 2.1 The problem

AI model APIs have problems often. Most are **partial**, affecting specific models, features or regions, rather than full outages. Data gathered on 1 October 2026:

- **OpenAI:** 11 incidents in January 2026, about one every 2.5 days, mostly resolved in 30 to 90 minutes ([TierZero](https://www.tierzero.ai/blog/ai-provider-outages-2026/)).
- **Anthropic, January 2026:** multiple incidents a week in late January, including a model-specific incident with a 30-hour resolution cycle (same source).
- **Anthropic, September 2026:** incidents on the 15th, 22nd and 29th, several affecting only "certain models" ([API Status Check](https://apistatuscheck.com/api/anthropic)).
- **Compared with other APIs:** a 2026 reliability report covering 215+ APIs ranked AI/ML APIs as the least reliable category ([Nordic APIs](https://nordicapis.com/api-reliability-report-2026-uptime-patterns-across-215-services/)).
- **Academic study:** Anthropic incidents are more frequent but resolved faster. OpenAI services show better failure isolation, meaning more of their incidents are partial ([arXiv 2501.12469](https://arxiv.org/html/2501.12469v2)).

**Why developers struggle with this**

- When an AI feature slows down or breaks, the developer cannot easily tell whether the cause is their code, their account (rate limits, billing) or the provider.
- Official status pages are coarse and can lag behind real problems.
- Existing "is it down?" sites ping endpoints or scrape status pages. That misses partial and workload-specific degradation, for example when short calls are fine but 20,000-token calls are slow.
- Apps and agents often depend on **several models across several providers and routes**: direct API, AWS Bedrock, Google Vertex AI, Azure OpenAI. The same model reached through different routes runs on different infrastructure and can fail independently.

### 2.2 Use cases

1. **"My chatbot is slow."** The developer opens the dashboard and sees that their p95 time to first token on a Claude model has tripled in the last 20 minutes. The anonymised baseline shows the same thing for everyone in the EU, so the problem is on the provider's side. This saves an hour of debugging their own code.
2. **"Is it me?"** The errors are 401s and 400s, labelled "likely your side" (auth or a bad request), and the global baseline is normal. The problem is in their code or account.
3. **Rate limiting.** A rising share of 429 errors. The dashboard shows whether other users see the same thing or whether it is specific to their account limits.
4. **Choosing a route or model.** A history of their own traffic per model and route, for example the same model through Bedrock compared with the direct API.
5. **Rolling out a new model.** The developer switches to a newly released model and immediately sees its real latency and error profile on their own workload.
6. **Later, paid:** alerts, plus scheduled checks that keep measuring while their app is idle (Section 8).

### 2.3 Why Saad is building it

- **Portfolio project** for graduate and early-career roles: AI engineer, SRE and observability, cloud, platform and infrastructure engineering, and forward-deployed engineering.
- **Fills current CV gaps with real, hosted, demonstrable work:**
  - automated testing (pytest)
  - CI/CD (GitHub Actions)
  - infrastructure as code (Terraform)
  - OpenTelemetry
  - time-series data modelling
  - SDK and library design
  - multi-tenant SaaS security
  - optionally AWS (see Section 16)
- **Builds on existing strengths:**
  - Python, FastAPI, Postgres, Redis, Docker and Fly.io deployments.
  - Worker and queue design from his PDF-to-Video project: a Postgres `SELECT ... FOR UPDATE SKIP LOCKED` job queue and Redis credential brokering with a TTL.
  - Datadog and Grafana use during his Yahoo placement.
- **A real product** that can be hosted publicly, used by real developers and potentially monetised.
- **Rule:** nothing from this project goes on Saad's CV until it is actually built and running.

---

## 3. Decision history

This section exists so the agent does not drift back to ideas that were already rejected.

| Idea considered | Outcome | Reason |
|---|---|---|
| CV-tailoring tool with a "fact ledger" | Not pursued | Saad preferred an AI-infrastructure project |
| Model comparison where users upload their own prompts or eval sets | Rejected | Most users will not want to upload prompts |
| Public "Downdetector for AI" driven by **our own** test calls | Rejected | Probe costs land on us: roughly $100-150/month minimum, and $1,500+/month for broad model × route × region coverage. The "is it down?" market is also crowded: StatusGator, API Status Check ($9/month alerts), PulsAPI, Maxim's Bifrost status pages |
| Self-hosted open-model inference benchmarking (vLLM on rented GPUs) | Parked | GPU cost and hard to sell. Possible add-on later |
| **Passive SDK measuring the user's own calls, plus a hosted dashboard** | **Chosen** | No per-call cost to us. Data matches the user's real workload. No API keys handed over. Network effect from the anonymised baseline |
| Our own synthetic checks for every user | Dropped from the free tier | Becomes a paid feature that runs in the user's infrastructure with **their** key (Section 8) |
| "Open source" as the main differentiator | Not enough on its own | Langfuse, OpenLLMetry, Helicone and others are already open source. Differentiation is in Section 12 |

---

## 4. How it works, end to end

### 4.1 User journey (free tier)

1. **Sign up** on `app.<domain>` with GitHub or Google OAuth. There are no passwords.
2. **Create a project**, for example `my-chat-app`. Use one project per app, or one project with a `service` tag per component.
3. **Copy the ingest key.** It is shown once only, for example `rm_live_8f3k...`.
4. **Install and initialise:**

   ```bash
   pip install yourpkg
   export YOURPKG_KEY=rm_live_8f3k...
   ```

   ```python
   import yourpkg
   yourpkg.init(service="summariser", environment="prod")  # reads YOURPKG_KEY from the environment
   ```

5. **Run the app as normal.** The onboarding screen polls and shows "✓ First call received from `claude-…`" within seconds.
6. **Use the dashboard:** overview table, live feed, per-model pages, official incidents and the you-vs-everyone comparison.

`init()` detects which supported AI libraries are installed and wraps them. **No other code changes are needed.** Clients created before or after `init()` are both covered, because the SDK patches the library classes rather than individual client instances.

### 4.2 Data flow

```
┌──────────────────────── user's server ────────────────────────┐
│  app code ──► openai / anthropic / ... SDK call               │
│                  ▲  wrapped by yourpkg (timings, usage,        │
│                  │  status — NO content)                       │
│                  └─► per-minute summaries + sampled calls      │
│                         └─► background thread: samples ~5 s,   │
│                             summaries once a minute (gzip)     │
└───────────────────────────────┬────────────────────────────────┘
                                │ HTTPS POST /v1/ingest
                                │ Authorization: Bearer rm_live_...
                                ▼
                     ┌─────────────────────┐
                     │ Ingest API (FastAPI)│  validate key hash → project
                     │ stateless, scalable │  validate schema, limits
                     └──────────┬──────────┘  batch insert
                                ▼
                     ┌─────────────────────┐   ┌───────────────────┐
                     │ Postgres            │◄──│ Rollup worker      │ 1-min rollups,
                     │ summaries + samples │   │ (every minute)     │ histograms, global
                     │ rollups, status     │◄──│ Status poller      │ baseline (k-anon)
                     └──────────┬──────────┘   │ (every 60 s)       │
                                │              └───────────────────┘
                                ▼
                     ┌─────────────────────┐
                     │ Dashboard API       │  session auth, tenant-scoped
                     │ + React web app     │  live feed, charts, incidents
                     └─────────────────────┘
```

### 4.3 Components

| # | Component | Responsibility |
|---|---|---|
| 1 | **Python SDK** (`yourpkg`) | Wraps provider SDK calls and measures them. Folds measurements into per-minute summaries and ships those plus errors and a small sample of calls (Section 19). Zero runtime dependencies. Fails open |
| 2 | **Ingest API** | Authenticates the write-only key, validates and stores events. Stateless and horizontally scalable |
| 3 | **Database** | Summary records and sampled calls (short retention), 1-minute rollups, global rollups, accounts and keys, official status data |
| 4 | **Rollup worker** | Adds up the summary records from all of a project's app processes into per-minute rollups (mergeable histograms). Builds the anonymised global baseline |
| 5 | **Status poller** | Polls official provider status pages every 60 s and stores incidents and component states |
| 6 | **Dashboard API** | Session-authenticated, tenant-scoped read API plus project and key management |
| 7 | **Web app** | Landing page, onboarding, dashboard, live feed, model pages, settings |

---

## 5. Privacy model and invariants (non-negotiable)

Privacy is a core selling point, so treat it as a product feature, not just a rule.

### 5.1 Never collected, under any circumstances

- Prompt or message content, system prompts, and tool definitions, arguments or results.
- Outputs or completions, reasoning or thinking text, embedding inputs and vectors, and file or image contents.
- Provider API keys, `Authorization` headers and any other headers' values, except the specific numeric rate-limit headers listed in Section 6.4 (later phase).
- Full URLs, URL paths and query strings. Some Google endpoints accept the API key as a `?key=` query parameter, so only the **hostname** may ever be read.
- Provider error **message text**, which can echo input. Only the HTTP status and an allowlisted short error code are kept (Section 6.5).
- End-user identifiers passed to providers (for example OpenAI's `user` field), request metadata fields and custom headers.

### 5.2 Invariants

| ID | Invariant | How it is enforced |
|---|---|---|
| **P1** | No content ever leaves the user's process | SDK reads only timing, usage counts, status and model names. **Sentinel tests** (Section 6.9) put marker strings in prompts, outputs and keys and assert they never appear in any payload or log. On the server, the Pydantic schema uses `extra="forbid"` and only allows bounded identifier strings, so a content field cannot be stored even if an SDK bug sends one |
| **P2** | Hostnames only, and only for known public providers | Known provider hosts are sent as-is, with customer-specific subdomains stripped (for example `mycompany.openai.azure.com` → `openai.azure.com`). Local and private hosts are sent as route `local`, and any other host as route `custom`, **with no hostname at all**. Users can add their own label |
| **P3** | No extra calls, and never modify the user's requests or responses | For example, do **not** inject OpenAI's `stream_options={"include_usage": True}`. It adds a final chunk with an empty `choices` list, which breaks user code that reads `chunk.choices[0]` |
| **P4** | Fail open | The SDK never raises into user code and never blocks the call path. Memory is bounded and data is dropped rather than causing harm (Section 6.7) |
| **P5** | Transparency | `yourpkg.init(debug=True)` prints every payload locally before sending. `PRIVACY.md` documents every field. The SDK is open source |
| **P6** | The global comparison cannot reveal any one user | Global views show only normalised metrics (latency percentiles, error rates), **never call volumes**. A global cell is shown only when it has **≥ 5 distinct projects** in the window **and** no single project contributes more than 50% of its samples. `custom` and `local` routes are excluded. Projects can opt out |
| **P7** | No remote control of the SDK | Server responses can only accept, reject or ask the SDK to slow down. The server can never change what the SDK captures or make it run code |

---

## 6. The SDK (Python first)

### 6.1 Public API

```python
yourpkg.init(
    api_key=None,          # default: env YOURPKG_KEY
    service=None,          # user tag, e.g. "summariser"
    environment=None,      # user tag, e.g. "prod"
    endpoint="https://ingest.<domain>",  # env YOURPKG_ENDPOINT (for local dev / self-host)
    enabled=True,          # env YOURPKG_ENABLED=false is a kill switch: no patching at all
    debug=False,           # env YOURPKG_DEBUG: print every payload locally before sending
    flush_interval=5.0,    # seconds
    max_batch=200,         # events per request
    max_queue=10_000,      # bounded in-memory queue
    sample_rate=0.05,      # share of successful calls sent individually (Section 19)
    max_samples_per_minute=10,  # per-process cap on random samples
    origin_region=None,    # else auto-detected from env (FLY_REGION, AWS_REGION, ...)
    instrument=None,       # default: every supported library that is installed
)
yourpkg.flush(timeout=2.0)     # for scripts and serverless handlers
yourpkg.shutdown(timeout=2.0)  # flush + stop; also registered with atexit
yourpkg.track(...)             # manual instrumentation (see below)
```

**Behaviour of `init()`**

- `init()` is idempotent. A second call never double-patches.
- With no key, it logs **one** warning and does nothing. It never raises.
- With `debug=True` and no key, it prints payloads without sending them, which is useful for auditing.

**Manual instrumentation** covers anything not supported automatically, such as raw HTTP calls or in-house model servers:

```python
with yourpkg.track(provider="custom", model="my-finetune", label="internal-llm", streaming=True) as t:
    for chunk in call_my_model(...):
        t.first_token()            # idempotent, records the first call only
        ...
    t.set_usage(input_tokens=1234, output_tokens=210)
# An exception inside the block is recorded (mapped to error_class) and re-raised unchanged.
```

### 6.2 Supported call paths

| Phase | Library | Methods | Notes |
|---|---|---|---|
| **MVP** | `openai` (v1+) | `chat.completions.create`, `responses.create`, sync and async, streaming and non-streaming | Also covers the `AzureOpenAI` client and **every OpenAI-compatible provider** used via `base_url` (Groq, Mistral, DeepSeek, OpenRouter, Together, Fireworks, xAI, Ollama, vLLM, ...). The provider is detected from the host |
| **MVP** | `anthropic` | `messages.create` (sync/async, `stream=True/False`) and the `messages.stream(...)` helper | Also covers the `AnthropicBedrock` and `AnthropicVertex` clients, detected from the host |
| Later (free) | `openai` | `embeddings.create`, structured-output parse helpers, `chat.completions.stream` helper | Verify current helper names |
| Later (free) | `google-genai` | `models.generate_content`, `generate_content_stream` | Gemini API and Vertex modes |
| Later (free) | `boto3` Bedrock Runtime | `converse`, `converse_stream`, `invoke_model`, `invoke_model_with_response_stream` | Via botocore event hooks or method wrapping |
| Later (free) | Frameworks: LangChain, LlamaIndex, LiteLLM, agent SDKs | n/a | Most call the provider SDKs underneath, so they are covered automatically. **Verify each with an example app.** LiteLLM in particular may call some providers over raw HTTP |
| Later (free) | **TypeScript SDK** (npm) | Equivalent coverage | After the Python SDK is solid |
| Later (free) | **OpenTelemetry OTLP ingest** | GenAI semantic-convention spans | Lets OpenLLMetry and other OTel users send data by changing one exporter setting. **Strip any content attributes or events** before storage (P1) |

**Out of scope**

- Tools the user does not own, such as Claude Code, Cursor or the ChatGPT app.
- Browser-side calls. Provider keys and ingest keys must never be in browser code anyway.

### 6.3 Instrumentation approach

**Default: method-level wrapping.**

- Patch the provider SDK resource classes, for example the class behind `client.chat.completions.create`. Patching classes covers every client instance, including ones created before `init()`.
- This gives the logical call timing (including the SDK's internal retries), parsed usage, the returned model, the first content chunk of a stream, and exception types.

**Alternative considered: HTTP-transport-level hooks** (httpx event hooks, or wrapping `httpx.Client.send`).

- **Pros:** sees each retry attempt, raw status codes and rate-limit headers.
- **Cons:** must parse the SSE stream to find the first content token, and also sees the app's non-AI HTTP traffic.
- **Decision:** method-level for the MVP. A transport hook may be added later, **only** to read attempt counts and the numeric rate-limit headers. Record this choice in an ADR.

**Patching rules**

- Resolve module and class paths dynamically. They move between SDK versions, so if a path is not found, log once and skip that integration rather than failing.
- Idempotent: mark wrapped functions, keep references to the originals, and provide `_uninstrument()` for tests.
- Use `functools.wraps`. **Never change arguments or return values.**
- All measurement code is wrapped so it can never break the user's call:

```python
def wrapper(*args, **kwargs):
    rec = _safe(start_record, args, kwargs)       # never raises
    try:
        result = original(*args, **kwargs)
    except BaseException as exc:
        _safe(finish_error, rec, exc)
        raise                                     # original exception, untouched
    return _safe_wrap_result(rec, result)         # falls back to the raw result on any internal error
```

The same pattern applies for async methods.

**Streaming**

- Wrap the returned stream so that iteration (sync and async), attribute access, context-manager protocols and `close()` all behave exactly as before.
- Do not buffer or consume chunks.
- Prefer the least intrusive technique, for example wrapping the stream's internal iterator, so that `isinstance(stream, openai.Stream)` in user code still passes. Investigate the options and record the choice in an ADR.

What the stream wrapper records:

- **First output chunk time:** the first chunk carrying output, whether a text delta, tool-call delta or reasoning/thinking delta. **Not** the first SSE event, because for example Anthropic's `message_start` arrives before any content.
- **Output chunk count:** used to estimate tokens if usage is never reported.
- **Usage, when the stream provides it:** Anthropic `message_start`/`message_delta`, the OpenAI Responses `response.completed` event, and OpenAI chat **only** if the user set `include_usage` themselves (see P3).
- **Finalisation:** on exhaustion, exception, `close()` or context exit. Streams abandoned without closing are finalised as `cancelled` via `weakref.finalize`, with no heavy logic in `__del__`.
- The Anthropic `messages.stream()` helper returns a context manager. Wrap the manager so the stream it yields is measured. Verify the current helper API.

**General mechanics**

- **Clocks:** `time.perf_counter_ns()` for durations and `time.time_ns()` for the wall-clock `ts`.
- **Enqueueing:** O(1) and non-blocking (`put_nowait`). If the queue is full, drop the event and increment a `dropped` counter.

### 6.4 Event schema

Each call produces one **call measurement** with the fields below. **Every field is metadata.**

Most measurements are **not sent individually**. The SDK folds them into per-minute **summary records** and sends individually only errors, the first call of each new series and a small random sample (Section 19). The fields below define a **sample record**. Section 19.2 defines the summary record.

| Field | Type | Notes |
|---|---|---|
| `event_id` | UUIDv4 string | Generated client-side. Used for idempotent inserts |
| `reason` | enum | Why this call was sent individually: `error`, `first_of_series` or `random` (Section 19) |
| `ts` | int, epoch ms | Wall-clock start of the call |
| `duration_ms` | float | Call start to return (non-streaming) or to the end of the stream |
| `ttft_ms` | float or null | Streaming only. Call start to first output chunk |
| `provider` | enum string | `openai`, `anthropic`, `google`, `mistral`, `groq`, `deepseek`, `openrouter`, `together`, `fireworks`, `xai`, `meta`, `amazon`, `custom`, `local`, ... |
| `route` | enum string | `direct`, `azure`, `bedrock`, `vertex`, `gemini_api`, `openai_compatible`, `custom`, `local` |
| `endpoint_host` | string or null | **Allowlisted public provider hosts only**, with customer subdomains stripped. Null for `custom` and `local` |
| `endpoint_region` | string or null | Parsed from the host where present (Bedrock and Vertex include it) |
| `operation` | enum string | `chat.completions`, `responses`, `messages`, `embeddings`, `generate_content`, `converse`, `manual` |
| `model_requested` | string | Model name as passed by the user |
| `model_returned` | string or null | Model name reported in the response (often a dated version) |
| `streaming` | bool | |
| `has_tools` | bool | True if the request included tool definitions. **Content is never read**, only whether tools were present |
| `input_tokens`, `output_tokens`, `cached_input_tokens` | int or null | From the provider's usage object |
| `output_tokens_estimated` | bool | True when usage was missing and the output chunk count was used instead |
| `status` | enum | `ok`, `error`, `cancelled` |
| `error_class` | enum or null | See Section 6.5 |
| `http_status` | int or null | |
| `provider_error_code` | string or null | **Allowlisted short codes only**, matching `^[a-z_]{1,64}$`, e.g. `insufficient_quota`, `overloaded_error`, `rate_limit_exceeded`. Never the message text |
| `service`, `environment` | string or null | User tags from `init()` |
| `label` | string or null | Optional user label, e.g. for a `custom` endpoint |
| `origin_region` | string or null | The **user's app** region, from `FLY_REGION`, `AWS_REGION`, `VERCEL_REGION`, etc. or config |
| `provider_sdk`, `provider_sdk_version` | string | e.g. `anthropic`, `0.x.y` |
| *(later)* `rl_remaining_requests`, `rl_remaining_tokens` | int or null | Numeric values from rate-limit headers only, once a transport hook exists |

**Batch envelope example:**

```json
{
  "schema_version": 1,
  "sdk": { "name": "yourpkg-python", "version": "0.1.0" },
  "runtime": { "language": "python", "version": "3.12.4" },
  "sent_at": 1790000000123,
  "dropped_since_last": 0,
  "summaries": [],
  "samples": [
    {
      "event_id": "9b2f6c2e-1d0a-4b7e-9a51-3f0f2a7c8d11",
      "reason": "random",
      "ts": 1790000000000,
      "duration_ms": 2843.1,
      "ttft_ms": 612.4,
      "provider": "anthropic",
      "route": "direct",
      "endpoint_host": "api.anthropic.com",
      "endpoint_region": null,
      "operation": "messages",
      "model_requested": "claude-sonnet-x",
      "model_returned": "claude-sonnet-x-20260801",
      "streaming": true,
      "has_tools": false,
      "input_tokens": 18234,
      "output_tokens": 412,
      "cached_input_tokens": 0,
      "output_tokens_estimated": false,
      "status": "ok",
      "error_class": null,
      "http_status": 200,
      "provider_error_code": null,
      "service": "summariser",
      "environment": "prod",
      "label": null,
      "origin_region": "lhr",
      "provider_sdk": "anthropic",
      "provider_sdk_version": "0.x.y"
    }
  ]
}
```

The `summaries` array is shown empty here. See Section 19.2 for a summary record.

**Fields derived on the server** for samples (never sent by the SDK): `project_id`, `received_at`, `input_bucket`, `origin_continent`, `model_key` (normalised model family, Section 9.4). Summary records already carry `input_bucket`, because the SDK groups by it.

**Server-side validation**

- Enums are strict.
- `model_*` must match `^[A-Za-z0-9._:/@+-]{1,128}$`. `service`, `environment` and `label` must match `^[A-Za-z0-9._:-]{1,64}$`.
- `0 ≤ duration_ms ≤ 3,600,000`, and `ttft_ms ≤ duration_ms` plus a small tolerance.
- Token counts must be between 0 and 10,000,000.
- `ts` must fall within [now − 24 h, now + 5 min].
- Invalid records are rejected individually and the rest of the batch is accepted.

### 6.5 Measurement definitions

**Timing metrics**

- **`duration_ms`:** from just before the provider SDK method is called until it returns (non-streaming), or until the stream finishes or closes (streaming).
  - Includes the provider SDK's built-in retries. Both the openai and anthropic SDKs retry by default; check the current defaults.
  - **Caveat:** for streams, chunk arrival is observed when the user's code pulls each chunk. Heavy per-chunk processing in user code inflates `duration_ms` and lowers the measured decode rate. Document this.
- **`ttft_ms`, time to first token:** streaming only. Measured from the call start to the first output chunk.
  - Reasoning models can have long TTFT when their thinking is not streamed. All comparisons are per model, so this stays fair, but document it.
- **Decode rate, output tokens per second:** computed on the server for streaming calls with `output_tokens ≥ 16` and no estimate flag, as `output_tokens / ((duration_ms − ttft_ms) / 1000)`.
  - Not computed for non-streaming calls. Those show total latency per input bucket instead.

**Input buckets** (agreed starting point, refine with data)

- `xs`: under 1k input tokens
- `s`: 1k-10k
- `l`: 10k and above
- `unknown`: when there is no usage data

Long inputs are naturally slower, so "slow" always means **slower than this project's own baseline for the same model, route and input bucket**.

**Error classes and "whose side" hints**

| `error_class` | Typical trigger | Hint shown to the user |
|---|---|---|
| `rate_limited` | HTTP 429 with a rate-limit code | "Your limits or provider capacity. Check the global baseline" |
| `quota_error` | Billing or quota exhausted (OpenAI returns 429 with `insufficient_quota`, which is why the code is needed) | "Likely your side (billing or quota)" |
| `auth_error` | 401, 403 | "Likely your side (key or permissions)" |
| `client_error` | 400, 404, 409, 413, 422 | "Likely your side (request rejected)" |
| `overloaded` | Anthropic 529 `overloaded_error`, 503 overloaded | "Provider capacity issue" |
| `server_error` | 500, 502, 503, 504 | "Likely provider side, especially if global is also elevated" |
| `timeout` | Client or server timeout | "Slow provider or network" |
| `connection_error` | DNS, TLS, connection reset | "Network between you and the provider" |
| `cancelled` | Stream abandoned or task cancelled | Not counted as an error rate |
| `unknown` | Anything else | none |

Map errors from SDK exception classes and status codes, never from message text.

### 6.6 Provider and route detection (by hostname)

| Host pattern | `provider` | `route` | `endpoint_host` sent |
|---|---|---|---|
| `api.openai.com` | openai | direct | as-is |
| `*.openai.azure.com`, `*.services.ai.azure.com`, `*.cognitiveservices.azure.com` | openai | azure | **subdomain stripped**, because it contains the customer's resource name |
| `api.anthropic.com` | anthropic | direct | as-is |
| `bedrock-runtime.<region>.amazonaws.com` | from model ID prefix (`anthropic.`, `meta.`, `amazon.`, `mistral.`, ...) | bedrock | as-is, region parsed |
| `<region>-aiplatform.googleapis.com`, `aiplatform.googleapis.com` | google or anthropic (from the model/publisher) | vertex | as-is, region parsed |
| `generativelanguage.googleapis.com` | google | gemini_api | as-is |
| `api.groq.com`, `api.mistral.ai`, `api.deepseek.com`, `openrouter.ai`, `api.together.xyz`, `api.fireworks.ai`, `api.x.ai` | matching provider | openai_compatible or direct | as-is |
| `localhost`, `127.0.0.1`, `::1`, private IP ranges, `*.local`, `*.internal` | local | local | **null** |
| anything else | custom | custom | **null** (user may set a `label`) |

Keep the allowlist in one shared module in the SDK and re-validate it on the server. Verify the host patterns against current provider docs.

### 6.7 Transport and resilience (fail-open design)

**Sending**

- A background daemon thread starts lazily on the first call. It sends queued samples every `flush_interval` seconds (or when `max_batch` are queued) and closed per-minute summaries once a minute (Section 19.3). **No network I/O ever happens on the caller's thread.**
- The queue is bounded (`max_queue`). On overflow, drop and count the dropped events, and report the count as `dropped_since_last`.
- HTTP uses the stdlib only (`urllib.request` / `http.client`) with TLS verification on, a ~3 s connect and ~5 s total timeout, a gzip body and `User-Agent: yourpkg-python/<version>`. Respect `HTTPS_PROXY`.

**Handling responses from the ingest API**

| Response | SDK behaviour |
|---|---|
| `202` | Done |
| `400` | Drop the batch and log once |
| `401` / `403` | Key invalid or revoked: **stop sending for this process** and log one warning |
| `413` | Split the batch in half and retry once |
| `429` | Respect `Retry-After` (capped at 5 min) and keep the bounded queue |
| `5xx` / network error | Retry with exponential backoff and jitter (~1 s, 2 s, 4 s), then drop the batch |

**Lifecycle**

- **Exit:** an `atexit` handler flushes with a 2 s cap.
- **Forking:** `os.register_at_fork(after_in_child=...)` resets the queue and thread in child processes, which matters under gunicorn or uwsgi with preload.
- **Serverless** (e.g. AWS Lambda): background threads freeze between invocations. Document calling `yourpkg.flush()` at the end of the handler, and add a decorator helper later.

**Hygiene**

- **Logging:** use `logging.getLogger("yourpkg")` with a `NullHandler`. Rate-limit warnings to once per type per 10 minutes, and never print unless `debug=True`.
- **Runtime support:** Python 3.10+, but verify which versions current provider SDKs support.
- **Zero runtime dependencies**, which minimises version conflicts and supply-chain risk.

### 6.8 Performance budget

These are targets. Measure and publish the real numbers, which are also good interview material.

- **Call-path overhead:** under 50 µs median added per call, and under 2 µs per streamed chunk. Measured by a CI benchmark against a mocked provider, with a regression threshold.
- **Memory:** bounded by `max_queue` and the 500-series cap (Section 19.3), roughly 10 MB in the worst case.
- **Network:** never on the caller's thread.

### 6.9 SDK test requirements

- **Functional matrix:** every supported method × sync/async × streaming/non-streaming × success and each error class, using mocked HTTP (e.g. `respx` for httpx-based SDKs).
- **Privacy sentinel tests (P1, P2):**
  - Inject marker strings everywhere they could leak from:
    - prompts and system prompts (`SENTINEL_PROMPT_7f3a`)
    - tool arguments
    - mocked outputs (`SENTINEL_OUTPUT_91c2`)
    - the API key (`sk-SENTINEL-KEY`)
    - an Azure resource subdomain (`sentinelcorp`)
    - a `base_url` path and query (`?key=SENTINEL_QS`)
    - provider error messages
  - Assert **none** appear in any serialised payload, debug output or log line.
- **Fail-open tests (P4):**
  - Ingest unreachable, timing out, returning 500/401/429 or garbage.
  - Faults injected into SDK internals.
  - In all cases the user's call returns an identical result or raises the identical exception, and no SDK exception escapes.
- **Transparency tests (P3):** results and streams keep their type, `isinstance` checks pass, and every public stream behaviour is preserved.
- **Compatibility matrix:** nox or tox sessions across a range of `openai` and `anthropic` versions (oldest supported to latest) and Python versions.
- **Overhead benchmark** in CI, plus fork, shutdown and flush tests.

### 6.10 Packaging and release

- `pyproject.toml` with a src layout, full type hints, `py.typed`, ruff and strict mypy.
- **Licence:** Apache-2.0 for the SDK. It includes an explicit patent grant and is common for SDKs. Confirm in Section 16.
- **Publishing:** to PyPI from GitHub Actions using **Trusted Publishing** (OIDC, no long-lived tokens), with build attestations.
- **Versioning:** semantic versioning, a `CHANGELOG.md` and tag-triggered releases (`sdk-v*`).

---

## 7. Free tier (build this now)

### 7.1 Features

1. Sign-in with GitHub or Google OAuth.
2. Projects. Placeholder limit: 3 per account.
3. Ingest keys: create (shown once), list (prefix and last 4 characters, created date, last used), revoke. Multiple keys per project allow zero-downtime rotation.
4. Python SDK with passive capture (Section 6).
5. Onboarding wizard with a live "waiting for first call" check.
6. **Overview:** one row per model and route over the selected range, showing:
   - calls
   - error rate, split into provider side and your side
   - p50/p95 TTFT and p50/p95 duration
   - decode rate
   - a sparkline, a health badge and a vs-everyone indicator
7. **Live feed** of recent calls, refreshing automatically: every error plus a labelled sample of successful calls (metadata only, Section 19).
8. **Model detail page:** TTFT, duration, decode rate and error rate by class over 1 h, 24 h and 7 d, split by input bucket. Official incidents are shaded and the global baseline is overlaid.
9. **Health badges**, display only with no notifications. Rules in 7.3.
10. **"Whose side?" hints** for each error class (Section 6.5).
11. **Official provider status:** a banner for active incidents on providers the project uses, plus an incident timeline.
12. **Anonymised global baseline** ("you vs everyone"), with a sharing toggle per project.
13. **Filters:** service, environment, route, origin region.
14. **Deletion:** delete a project or the whole account, which hard-deletes the data.

### 7.2 Limits

All numbers are placeholders (Section 16).

| Limit | Placeholder |
|---|---|
| Calls measured per account | 1M/month (cheap to serve because of Section 19) |
| Retention | 7 days (summaries, samples and rollups) |
| Projects | 3 |

**Over quota**

- Ingest returns `429` with `Retry-After` until the quota resets.
- The SDK backs off and drops events.
- The dashboard shows a banner.
- **The user's app is never affected.**

**Storage cost check:** with summaries and sampling (Section 19), an account at the 1M-calls limit using two models holds roughly 30k records at any time under 7-day retention, about 20 MB before indexes. Verify with real data.

### 7.3 Health badge rules

These are starting points. Tune them once there is real data.

- **Window:** last 15 minutes. **Baseline:** trailing 7 days for the same project, model, route and input bucket.
  - With under 24 h of history, show "baseline building".
- **Minimum samples:** 20 calls in the window, otherwise show "not enough data".
- **Slower than usual:**
  - Amber when the window's p50 TTFT is more than 1.5× the baseline p50. For non-streaming calls, use p50 duration.
  - Red when it is more than 2.5×.
- **Elevated errors:** provider-side error rate (`rate_limited`, `overloaded`, `server_error`, `timeout`, `connection_error`) above max(2%, 3× baseline).
- **Your-side errors** (`auth_error`, `client_error`, `quota_error`) are shown separately and **never** turn the provider badge red.

---

## 8. Paid tier (parked: do not build yet)

### 8.1 Planned paid features

**1. Scheduled active checks**

These run **inside the user's infrastructure with their own provider key**, as an SDK module or a small container. Only results come back to us.

- They run only when there has been no real traffic for N minutes, and stop at a user-set monthly spend cap.
- They use dummy text only, never user data.
- They also exercise backup or failover models that get no real traffic.

| Check | What it sends | What it catches | Approx. cost per model |
|---|---|---|---|
| **Basic health check** | ~20 input tokens, ~5 output | Outages, errors, rough latency | About $1-2/month at 5-minute intervals on a mid-priced model (estimate) |
| **Workload-shaped check** | Dummy text matching the user's typical input-size bucket and settings (streaming, tool use) | Slowdowns that only affect heavy calls, which a basic check misses | Depends on size, from a few dollars to tens of dollars a month |

**2. Alerts:** Slack, email, PagerDuty and webhooks, with custom thresholds (e.g. "TTFT p95 > 2 s for 5 min") and alerts on official incidents for providers in use.

**3. Longer retention:** 30, 90 or 365 days.

**4. Teams:** members, roles, invites, and SSO later.

**5. Higher event volumes.**

**6. Custom endpoint monitoring** for self-hosted or fine-tuned models, through a container the user runs with their key that sends back results only.

**7. Data API and export:** CSV/Parquet, and Prometheus/OTel export.

**8. Possibly** an embeddable status badge for the user's own app.

**Pricing:** to be decided. Validate demand with a waitlist **before** building any paid feature.

### 8.2 Hooks to leave in the free-tier design

- A `plan` column on users (default `free`) and a single `entitlements(plan)` function that returns limits: retention days, monthly events, projects.
- Retention and quotas are read from `entitlements`, never hard-coded.
- A modular SDK structure so a `checks` module can be added later.
- A landing-page "Coming soon: alerts and scheduled checks. Join the waitlist." This only stores emails, and the privacy policy must cover it.

---

## 9. Backend and infrastructure

### 9.1 Default stack

| Layer | Default | Notes |
|---|---|---|
| Backend | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 (or `asyncpg` directly on the ingest hot path), Alembic | Saad's strongest stack |
| Frontend | React + TypeScript + Vite SPA, with a charting library (uPlot or ECharts; Recharts acceptable) | Alternative: Next.js (Section 16) |
| Database | Postgres 16+ | Storage layout is an open decision (Section 9.4) |
| Cache / pub-sub | None in the MVP. Redis (e.g. Upstash) later for the key cache and live-feed fan-out | |
| Hosting | Fly.io, EU region | Alternative: AWS (Section 16) |
| Edge | Cloudflare: DNS, TLS, WAF, rate limiting, DDoS protection | |
| Infrastructure as code | Terraform | Fills a CV gap |
| CI/CD | GitHub Actions | Fills a CV gap |
| Auth | OAuth (GitHub, Google) via Authlib, with server-side sessions | No passwords stored |

### 9.2 Ingest API

`POST https://ingest.<domain>/v1/ingest`

**Headers:** `Authorization: Bearer rm_live_...`, `Content-Type: application/json`, optional `Content-Encoding: gzip`, `User-Agent`.

**Limits:** ≤ 1 MB compressed, ≤ 5 MB decompressed (stream-decompress with a hard cap to guard against zip bombs), ≤ 1,000 records (summaries plus samples) per batch.

**Processing steps:**

1. Check the key format and checksum. Malformed keys get `401` without touching the database.
2. Hash the key with SHA-256 and look it up (in-memory cache, maximum 60 s, so revocation takes effect within a minute). This gives the project and its revoked state.
3. Apply the per-key rate limit and the account quota check.
4. Parse and validate with Pydantic (`extra="forbid"`). Reject invalid records individually.
5. Derive the server fields: `project_id`, `received_at`, `origin_continent` and `model_key`, plus `input_bucket` for samples.
6. **Append-only** batch insert (`COPY`): summaries into `summary_records`, samples into `call_samples`, idempotent on their IDs. No updates on the hot path.
7. Update `ingest_keys.last_used_at`, debounced.

**Responses:**

- `202 {"accepted": n, "rejected": m, "errors": [{"index": i, "reason": "..."}]}`, with errors truncated to 20.
- `400` malformed envelope.
- `401` invalid or revoked key.
- `413` too large.
- `429` rate or quota exceeded, with `Retry-After`.
- `5xx` server error.

**Logging:** record project ID, counts, latency and status. **Never log request bodies, keys or `Authorization` headers.**

**Health checks:** `/healthz` (liveness) and `/readyz` (database reachable).

### 9.3 Dashboard API

Cookie session auth. **Every route is scoped to projects the user is a member of.**

**Auth and account**

- `GET /auth/{provider}/login`, `GET /auth/{provider}/callback`, `POST /api/auth/logout`
- `GET /api/me`, `DELETE /api/me` (account deletion)

**Projects and keys**

- `GET|POST /api/projects`, `PATCH|DELETE /api/projects/{id}` (settings, including the sharing toggle)
- `GET|POST /api/projects/{id}/keys`, `DELETE /api/projects/{id}/keys/{key_id}`
- `GET /api/projects/{id}/onboarding/first-event`

**Metrics**

- `GET /api/projects/{id}/overview?range=1h|24h|7d&service=&environment=&route=&origin_region=`
- `GET /api/projects/{id}/series?model=&route=&metric=&bucket=&range=`
  - Metrics: `ttft_p50|ttft_p95|duration_p50|duration_p95|decode_p50|error_rate|error_rate_by_class`
- `GET /api/projects/{id}/feed?after=<cursor>&limit=100`
- `GET /api/status/incidents?providers=&range=`
- `GET /api/global/series?provider=&route=&model=&bucket=&continent=&metric=&range=` (returns only points that meet P6)

**Chart resolution by range:** 1 h → 1-minute points, 24 h → 5-minute points, 7 d → 1-hour points. Charts are always served from rollups, never by scanning raw records.

### 9.4 Storage design

**Default, pending the decision in Section 16:**

- Plain Postgres with the `summary_records` and `call_samples` tables **range-partitioned by day**.
- Create partitions ahead of time with pg_partman or a small job.
- **Retention:** drop partitions older than the plan's retention, which is cheap and avoids vacuum churn.

**Alternatives**

- **TimescaleDB** (hypertables, continuous aggregates, retention policies) means less code. However, check host support: some managed Postgres providers only offer TimescaleDB's Apache-licensed subset, which excludes continuous aggregates and compression.
- **ClickHouse** for events, if volume ever demands it.

Keep queries behind a small repository layer so the storage engine can be swapped later.

**Core tables**

**Accounts and sessions**

- `users`: `id` (uuid), `email`, `display_name`, `plan` (default `free`), `created_at`, `deleted_at`
- `oauth_identities`: `id`, `user_id`, `provider`, `provider_subject`, `email_verified`. **Unique on `(provider, provider_subject)`.**
- `sessions`: `id_hash` (hash of a 256-bit random token), `user_id`, `created_at`, `last_seen_at`, `expires_at`, `user_agent`

**Projects and keys**

- `projects`: `id` (uuid), `name`, `owner_user_id`, `share_global` (bool, default per Section 16), `created_at`, `deleted_at`
- `project_members`: `project_id`, `user_id`, `role`. Only an `owner` role exists in the free tier, but the table is ready for teams.
- `ingest_keys`: `id`, `project_id`, `key_hash` (unique), `display_prefix`, `last4`, `created_by`, `created_at`, `last_used_at`, `revoked_at`

**Summaries, samples and rollups**

- `summary_records`, partitioned by `minute`: one row per summary received (Section 19.2). Append-only.
  - **Primary key:** `(project_id, minute, summary_id)`
- `call_samples`, partitioned by `ts`: the sample fields from Section 6.4 plus the server-derived fields. Feeds the live feed, recent errors and onboarding.
  - **Primary key:** `(project_id, ts, event_id)`
  - **Index:** `(project_id, ts DESC)` for the feed
- `rollups_1m` (built by summing `summary_records`): keyed by `(minute, project_id, service, environment, provider, route, model_key, input_bucket, origin_region)`. Columns:
  - `count`, `ok_count`, `cancelled_count`, and an error count for each class
  - `ttft_hist int[]`, `duration_hist int[]`, `decode_hist int[]`
  - `input_tokens_sum`, `output_tokens_sum`
- `global_rollups_5m` and `global_rollups_1h`: keyed by `(bucket_start, provider, route, endpoint_region, model_key, input_bucket, origin_continent)`. Columns:
  - `distinct_projects`, `max_project_share`
  - error counts and histograms
  - **Internal call counts are never exposed through the API.**

**Status data and reference tables**

- `status_sources`: `id`, `provider`, `url`, `format`, `enabled`, `etag`, `last_modified`, `last_polled_at`, `last_error`
- `status_incidents`: `id`, `source_id`, `external_id` (unique per source), `title`, `impact`, `status`, `started_at`, `resolved_at`, `updated_at`, `components text[]`, `url`
- `status_component_map`: `source_id`, `component_name` → `provider`, `route`, `model_pattern`
- `model_aliases`: `pattern` → `model_key`, which normalises dated versions to a family for global comparison. Maintained as config in the repo.
- `audit_log`: `id`, `user_id`, `project_id`, `action`, `created_at`, `metadata jsonb` (never secrets)
- `waitlist` (later): `email`, `feature`, `created_at`

**Mergeable histograms**

Percentiles **cannot be averaged** across minutes or projects. Instead, store counts in **fixed, log-spaced buckets**:

- **Latency (ms):** 25, 50, 100, 200, 300, 500, 750, 1000, 1500, 2000, 3000, 5000, 7500, 10000, 15000, 20000, 30000, 60000, 120000, +inf
- **Decode rate (tokens/s):** 5, 10, 20, 30, 40, 60, 80, 100, 150, 200, 300, 500, 1000, +inf

Adding arrays together gives exact bucket counts for any time range or group of projects. Percentiles are then interpolated within the bucket, with error bounded by bucket width. Document this trade-off; it is a strong interview topic.

### 9.5 Rollup worker

**Schedule:** runs every 60 s and recomputes `rollups_1m` for the minutes in [now − 15 min, now − 1 min] by summing the `summary_records` from all of a project's app processes, upserting the results. Recomputing whole minutes makes it idempotent. The 15-minute window covers the SDK's longest send delay (the 5-minute `Retry-After` cap plus retries).

**Single active worker:** uses a Postgres advisory lock, or job rows claimed with `SELECT ... FOR UPDATE SKIP LOCKED`, the pattern Saad already used in PDF-to-Video.

**Global rollups:**

- Built at 5-minute and 1-hour grains directly from project rollups.
- Only projects with `share_global = true` are included, and `custom` and `local` routes are excluded.
- `distinct_projects` and `max_project_share` are computed per grain. Distinct counts **are not additive**, so the API serves only those grains and does not sum smaller ones.
- The API hides any point that fails P6 (fewer than 5 distinct projects, or one project over 50% of samples). The chart shows a gap there.

**Retention job:** drops expired partitions and old rollups according to entitlements.

### 9.6 Status poller

**Sources:** config in the repo, synced to `status_sources`. Include every provider that has a status page: OpenAI, Anthropic, Google (Gemini API, and Vertex AI via Google Cloud status), AWS (Bedrock via the AWS Health Dashboard), Azure (Azure OpenAI), Mistral, Groq, DeepSeek, OpenRouter, Together, Fireworks and xAI.

**Adapters:**

- Many providers use Atlassian Statuspage, which exposes `/api/v2/summary.json`, `/api/v2/incidents.json` and `/api/v2/components.json`. Others offer RSS/Atom or custom JSON.
- Write one adapter per format (`statuspage_v2`, `rss`, `atom`, `gcp_incidents_json`, ...).
- **Verify each source's current format at build time.** Do not scrape HTML unless no feed exists and the site's terms allow it.

**Polling:**

- Poll every 60 s using `If-None-Match` / `If-Modified-Since`.
- Use per-source timeouts and backoff, so one failing source never blocks the others.

**Safety:** treat all fetched text as **untrusted**. Store it as plain text, cap its length and always render it escaped.

**Mapping:** `status_component_map` maps each status component to our provider, route and model pattern, so incidents appear on the right charts.

**Later:** measure how often the global baseline detects a degradation before the official status page reports it. **Publish this only once it has actually been measured.**

### 9.7 Live feed

- **MVP:** the client polls `GET /feed?after=<cursor>` every 3-5 s. This is simple and robust.
- **Later:** Server-Sent Events fed by Redis pub/sub from the ingest service.
- **Source:** `call_samples`, so the feed shows every error plus a labelled sample of successful calls, not every call.
- **Columns:** time, service, model, route, streaming, input tokens, output tokens, TTFT, duration, status or error class.

### 9.8 Hosting and deployment

This describes the default, Fly.io. AWS is the alternative in Section 16.

**Apps**

- `ingest`: 2+ machines, autoscaled, stateless.
- `web-api`: the dashboard API. The SPA is served from this app or from Cloudflare Pages.
- `worker`: rollups, retention and the status poller. One active instance, protected by the advisory lock.

**Data and network**

- **Region:** EU, Frankfurt or Amsterdam (decision D12). London is outside the EU. EU data residency is also a selling point for European developers.
- **Database:** managed Postgres with automated backups and point-in-time recovery. Test a restore before launch.
- **Hostnames:** `app.<domain>` and `ingest.<domain>` are separate, both behind Cloudflare.
- **Secrets:** kept in the platform secret store (Fly secrets or AWS Secrets Manager), never in the repo or in images.

**Build and provisioning**

- **Docker images:** multi-stage builds, slim and pinned base images, running as a non-root user. Saad previously cut a FastAPI image to 94 MB, so reuse that experience.
- **Terraform:** manages Cloudflare (DNS, WAF and rate-limit rules), the database where the provider supports Terraform, and any AWS resources. Fly apps are deployed with `flyctl` from CI. Verify the current state of Fly's Terraform support before relying on it.
- **Environments:** `dev` (local docker compose), an optional small `staging`, and `prod`.

### 9.9 CI/CD (GitHub Actions)

**On every pull request**

- Linting and type checks: ruff, mypy, eslint/prettier, tsc.
- Unit tests: the SDK matrix, server and web.
- Privacy sentinel tests and the overhead benchmark.
- Security scanning: `pip-audit`, `npm audit`, secret scanning, CodeQL or Semgrep.
- Terraform `fmt`, `validate` and `plan`, with the plan posted as a PR comment.
- Docker build.

**On merge to `main`**

1. Build and push images.
2. Run Alembic migrations as a release step.
3. Deploy to staging and run a smoke test.
4. Deploy to production, with manual approval at first.

**On tag `sdk-v*`:** build the SDK and publish it to PyPI via Trusted Publishing.

**Hardening:**

- Pin third-party actions to commit SHAs.
- Set least-privilege `permissions:` per job.
- Use OIDC for cloud access instead of long-lived keys.
- Turn on branch protection with required checks.

### 9.10 Observability of our own service

This is dogfooding, and good interview material.

- **Prometheus metrics:**
  - ingest requests by status, events accepted and rejected, batch size
  - ingest latency histogram and database insert latency
  - rollup lag (now minus the last processed minute)
  - status poller success per source
  - quota rejections
- **Dashboards and logs:** Grafana (Grafana Cloud free tier or self-hosted), structured JSON logs, and an external uptime check on `/healthz`.
- **Internal targets**, not promised to users: ingest availability 99.5% and rollup lag under 2 minutes.
- **Load test** the ingest API with k6 or Locust, and record the sustained events per second on the smallest production setup. Put the real number in the README.

### 9.11 Local development

- **Stack:** `docker compose up` starts Postgres, ingest, the API, the worker and the web dev server.
- **Fake provider server:** simulates latency, slow streams, 429, 529, 5xx and timeouts. It enables offline development, deterministic tests and demos without spending tokens.
- **`examples/`:**
  - OpenAI chat, streaming and non-streaming
  - an Anthropic summariser with long inputs
  - Azure, Bedrock and Vertex host detection, using mocks
  - manual `track()`
- **Tooling:** `.env.example`, Makefile targets (`make dev`, `make test`, `make lint`) and pre-commit hooks.

---

## 10. Website and dashboard

### 10.1 Public pages

- **Landing page:**
  - hero: "Is it you, or the model?"
  - the two-line install snippet
  - **"Exactly what we send"**: a real JSON payload
  - a you-vs-everyone chart
  - the privacy promise and the "what if your server is down?" answer
  - FAQ, a link to the GitHub repo, and the paid-features waitlist
- **Docs:** quickstart, supported libraries, configuration, "what we collect" (`PRIVACY.md`), debug mode for self-auditing, serverless notes, troubleshooting and FAQ.
- **Legal and security:** privacy policy, terms, a `/security` page and `/.well-known/security.txt`.
- **Later:** a **public status page** combining official incidents with anonymised crowd metrics that meet P6. This is a major marketing asset (Section 13), but only once there is enough data and provider terms have been checked.

### 10.2 App pages

- **Onboarding wizard:**
  1. Create the project.
  2. Show the key once, with a copy button and a "store this now" warning.
  3. Show install tabs: pip, environment variable, code.
  4. Wait for the first call, live.
  5. Show success and link to the dashboard.
- **Overview:**
  - range picker (1 h, 24 h, 7 d) and filters
  - health summary bar and active-incident banner
  - model × route table with sparklines, badges and the vs-everyone indicator ("everyone: normal", "everyone: also slow" or "not enough data")
- **Live feed.**
- **Model detail:**
  - a chart per metric, split by input bucket
  - error-class breakdown with "whose side" hints
  - official incidents shaded
  - the global baseline as an overlay line
- **Status:** incidents for providers this project uses, plus an "all providers" tab.
- **Settings:**
  - project: rename, the sharing toggle with a plain-English explanation, delete
  - keys: create, revoke, last used
  - account: connected identities, delete account

**Visual style:** dense, dark-first dashboard, somewhere between a status page and Grafana. The overview must be readable on a phone. Never rely on red and green alone; always add icons or labels for accessibility.

### 10.3 Display rules

- Always show the sample count. Never show a percentile computed from fewer than 20 samples; show "not enough data" instead.
- Label estimated values as estimates.
- Use the same units everywhere: ms and tokens/s.
- Show the global baseline only when P6 is met. Otherwise show "baseline not available yet".
- Keep hint wording neutral: "likely your side", never "your fault".

---

## 11. Security

Users trust this product in two ways:

- They put our SDK **inside their production apps**.
- They send us data about how their business uses AI.

Both must be protected. The checklist in 11.11 must be fully ticked before public launch.

### 11.1 Threat model

**Assets, most critical first**

1. **The integrity of users' production apps.** The SDK runs in their process, so a buggy or malicious release could harm every app using it.
2. **Users' metrics.** They reveal which models and providers a company uses, its call volumes and its growth, all of which are competitively sensitive.
3. **Account access** (dashboard sessions) and **ingest keys**.
4. **Our infrastructure secrets:** database credentials, OAuth client secrets, PyPI publishing rights, cloud and DNS accounts.
5. **Personal data:** account emails, names, IP addresses in logs, waitlist emails (GDPR).

**Actors and attacks**

| Actor | Example attacks | Main controls |
|---|---|---|
| Anonymous internet attacker | DDoS on ingest, vulnerability scanning, CSRF, clickjacking, open redirects | Cloudflare WAF and rate limits, security headers, CSRF protection, OAuth-only login (no passwords to steal) |
| Malicious registered user | IDOR (reading other projects), **stored XSS via ingested strings** (model names, tags, labels), quota abuse, project enumeration | Central tenant scoping plus IDOR tests, strict validation, output escaping and CSP, quotas, 404 for anything not theirs |
| Holder of a leaked ingest key | Flooding a project with junk data, exhausting its quota | Write-only key scope, per-key rate limits, revocation within 60 s |
| Supply-chain attacker | Malicious dependency, compromised CI action, PyPI account takeover | Zero-dependency SDK, Trusted Publishing, MFA, pinned actions, attestations |
| Data-inference attacker | Working out a competitor's traffic from the global baseline | P6: at least 5 projects per point, dominance rule, no volumes exposed, continent-level regions only |
| Malicious or compromised status feed | Script or HTML injected through incident titles | Treated as untrusted text, escaped, CSP |
| Operator error | Secrets or data leaking through logs, misconfiguration | No secrets or bodies in logs, least-privilege database roles, MFA everywhere |

### 11.2 Website and dashboard (browser security)

**Transport**

- HTTPS only, TLS 1.2+.
- HSTS: `max-age=31536000; includeSubDomains`, adding preload once stable.

**Security headers**

- CSP, starting from:

  ```
  default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self';
  font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'
  ```

  Loosen it only when strictly required. Never allow `unsafe-inline` for scripts; use nonces or hashes if a library needs inline styles.
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: strict-origin-when-cross-origin`
- a minimal `Permissions-Policy`
- `Cross-Origin-Opener-Policy: same-origin`

**XSS**

- React escapes by default. **Forbid `dangerouslySetInnerHTML`** with a lint rule.
- Treat every ingested string (model, service, environment, label) and every project name and status-feed text as untrusted: validate it on input and escape it on output.
- **Watch chart tooltips.** Some charting libraries render tooltips as HTML, so escape anything that comes from data.

**CSRF**

- SameSite=Lax cookies.
- Check the `Origin` header (or `Sec-Fetch-Site`) on every state-changing request.
- Use a CSRF token for any form posts.

**CORS**

- The dashboard API allows only `https://app.<domain>`, never a wildcard with credentials.
- **The ingest API has no CORS at all.** It is server-to-server only, which also discourages putting keys in browser code.

**Other controls**

- **Redirects:** after login, redirect only to relative internal paths.
- **Errors:** no stack traces in production responses, and debug mode off.
- **Rate limits:** per IP on auth endpoints, and per user on the API.
- **Destructive actions:** deleting a project or account, or revoking a key, requires confirmation (typing the project name) and is written to the audit log.
- **Key display:**
  - Show the key once with a copy button.
  - After that, show only the prefix and last 4 characters.
  - Warn clearly never to commit it or put it in front-end or mobile code.

### 11.3 Accounts, authentication and sessions

**Login**

- OAuth only (GitHub, Google) through a maintained library (Authlib). **No passwords are stored**, which removes password breaches and credential stuffing entirely.
- **OAuth flow:**
  - Use the `state` parameter and PKCE, with exact redirect URI matching.
  - Google: validate the ID token's signature, `iss`, `aud` and `exp`.
  - GitHub: fetch the **verified** primary email from the API.

**Identity linking**

- Link identities by `(provider, provider_subject)`, **never by email alone**. Matching on email would allow account takeover through an unverified email address on another provider.
- A user adds a second login provider explicitly from settings while signed in.

**Sessions**

- Sessions are server-side. The cookie is `__Host-session` with `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/` and no `Domain`.
- The cookie holds a 256-bit random token, and only its hash is stored.
- Timeouts: idle 7 days, absolute 30 days (placeholders).
- Rotate the session ID at login. Logout deletes the server-side session, and settings include "sign out of all sessions".

**Account protection**

- **Two-factor authentication** is handled by the OAuth provider. Encourage users to enable it on GitHub or Google.
- **Admin access:** no admin UI in the MVP. Any future admin UI goes on a separate hostname, behind separate auth, MFA and an IP allowlist, and every admin action is audit-logged.

### 11.4 Ingest keys

**Format**

- `rm_live_` followed by 32 random bytes (`secrets.token_bytes`) encoded as base62, then a 6-character checksum (e.g. CRC32 in base62).
- **Why this shape:** the prefix makes leaked keys recognisable to secret scanners (and later to GitHub's secret scanning partner programme), and the checksum lets the API reject garbage without a database lookup.

**Storage**

- Store **only the SHA-256 hash**, plus `display_prefix` and `last4`.
- A fast hash is correct for high-entropy random tokens. A slow password hash such as bcrypt adds no security here and would slow down every ingest request.

**Scope and lifecycle**

- **Write-only** and limited to **one project.** A key cannot read data or manage the project.
- Revocable instantly from the dashboard, taking effect within 60 s because of the cache.
- Multiple active keys per project allow rotation. `last_used_at` is shown so stale keys can be removed.
- Each key has a request rate limit, and the account has a monthly quota.

**Docs to give users**

- Keep keys in server environment variables or a secret manager.
- Never put them in browser or mobile code, and never commit them.
- If a key leaks, revoke it and create a new one.

**Later:** `rm_test_` keys for a sandbox project.

### 11.5 Ingest API hardening

- **Isolation:** its own hostname behind Cloudflare, with no cookies and no CORS.
- **Reject early, before any heavy work:**
  - check `Content-Length` (≤ 1 MB)
  - stream-decompress gzip with a hard 5 MB cap
  - check key format and checksum before any database work
  - apply a per-IP rate limit to invalid-key attempts
- **Strict schema** (Section 6.4): `extra="forbid"`, enums, regexes, numeric ranges and a timestamp window. This guarantees no content-like or oversized strings can be stored, and protects the dashboard from stored XSS.
- **SQL:** parameterised only. Never build SQL from strings.
- **Back-pressure:** request timeouts and bounded concurrency. If the database is slow, return `503` with `Retry-After` instead of queueing without limit.
- **Database role:** INSERT on events, and minimal SELECT on keys and projects. No other writes except `last_used_at`.

### 11.6 Multi-tenant isolation and data protection

**Tenant scoping**

- Every dashboard query gets its `project_id` through one central dependency, `require_project_member(project_id, user)`.
- Projects that don't exist and projects the user can't access **both return 404**, so attackers can't enumerate projects.
- All IDs are random UUIDs, never sequential.

**IDOR test suite**

- For every endpoint, user B must get 404 on user A's projects, keys, series and feed.
- Generate the suite from the route table, so a new endpoint cannot skip it.

**Defence in depth**

- **Postgres Row-Level Security** on project-scoped tables (decision D10).
- **Least-privilege database roles:** `ingest_writer`, `dashboard_app`, `worker`, `migrator`. The app never connects as a superuser.

**Encryption**

- TLS everywhere, including to the database.
- Managed encryption at rest and encrypted backups.

**Retention and deletion**

- Retention is enforced automatically by dropping partitions.
- Deleting a project or account hard-deletes its events, rollups and keys within 24 hours.
- Global rollups contain no project identifiers, so they remain. The privacy policy must say "anonymised aggregates are retained".
- Backups expire on a fixed schedule (e.g. 7-14 days), so deleted data eventually leaves backups too. Disclose this as well.

### 11.7 SDK and supply-chain security (protecting users' apps)

**Code safety**

- Zero runtime dependencies.
- Nothing happens at import time. Patching happens only in `init()`.
- No `eval`, `exec`, `pickle` or dynamic code loading.
- No remote configuration and no auto-update (P7).

**Network and environment**

- The only network activity is HTTPS POSTs to the configured endpoint.
- TLS verification cannot be turned off, except through an explicit, documented flag for `localhost` development.
- The SDK reads only its own `YOURPKG_*` variables and the documented region variables.

**Release process**

- PyPI Trusted Publishing from a protected GitHub environment with manual approval.
- MFA on PyPI and GitHub, build provenance attestations and signed tags.
- A changelog entry for every release. Version numbers are never reused, and a bad release is yanked and replaced.

**Ongoing**

- Register the chosen package name on PyPI and npm early, to prevent name squatting.
- Run the Section 6.9 privacy and fail-open tests on every PR.

### 11.8 Infrastructure, CI and secrets

**Accounts and domain**

- **MFA on every account:** GitHub, PyPI, npm, the cloud provider, Cloudflare, the domain registrar, email and Grafana.
- **Domain:** registrar lock on, plus DNSSEC if supported.

**Secrets**

- Secrets live only in secret stores. `.env` files are git-ignored, and GitHub secret scanning with push protection is on.

**GitHub Actions**

- Pin actions to commit SHAs and set least-privilege `permissions`.
- Use OIDC for cloud access, and protected environments for production deploys and PyPI publishing.
- Never expose secrets to workflows triggered by forked PRs. Do not run untrusted code with `pull_request_target`.

**Containers and network**

- **Containers:** non-root, minimal images, a read-only filesystem where possible, and image scanning (e.g. Trivy) in CI.
- **Database network:** not publicly reachable. Use private networking or strict IP restriction plus TLS. Admin access only through `fly proxy` or a bastion host.

**Maintenance**

- **Patching:** dependency updates weekly, with a target of patching critical CVEs within 72 hours.
- **Backups:** automated and encrypted. Test a restore before launch, then quarterly.

### 11.9 Privacy, compliance and legal

This is not legal advice. Get a professional review before charging money.

**GDPR**

- Saad operates from Ireland, so the **EU GDPR applies**, supervised by Ireland's Data Protection Commission.
- **Personal data held:** account emails and names, OAuth subject IDs, IP addresses in logs and waitlist emails.
- Event metadata about API calls is generally not personal data, but treat it as **confidential customer data** anyway.

**Required before public launch**

- **Privacy policy:** what is collected and why, lawful basis, retention, sub-processors, user rights.
- **Terms of service.**
- **Cookie notice,** only if non-essential cookies are used. Avoid them: no third-party analytics cookies, so use cookieless analytics or none.
- **Sub-processor list:** hosting, database, Cloudflare, OAuth providers, email.
- **Data export and deletion flows.**
- **EU hosting by default** (decision D12).

**Breaches:** GDPR can require notifying the supervisory authority within 72 hours of becoming aware of a personal-data breach. Keep a runbook ready.

**Later, before the paid tier:** a DPA (data processing agreement) template for business customers.

**AI provider terms:** before publishing any provider-level data (the public status page or monthly reports), check each provider's terms on publishing performance or benchmark data. Keep public data aggregated and factual.

**Global sharing:** explain the "share anonymised metrics" setting clearly during onboarding, with a one-click opt-out.

### 11.10 Vulnerability disclosure and incident response

**Disclosure:** a `SECURITY.md` in the repo and `/.well-known/security.txt` with a security contact email, promising acknowledgement within 72 hours.

**Runbooks**

| Incident | Response |
|---|---|
| Leaked ingest key | The user revokes it. We can also force-revoke |
| Leaked infrastructure secret | Rotate it and audit access |
| Bad or malicious SDK release | Yank it from PyPI, publish an advisory, release a fix |
| Database breach | Contain, assess, notify the DPC and users as required |
| DDoS | Cloudflare under-attack mode |

**Learning from incidents:** keep an incident log and write blameless post-mortems. These also make good interview material.

### 11.11 Launch security checklist

All items must be ticked before public launch.

**SDK**

- [ ] Privacy sentinel tests pass in CI for every supported integration
- [ ] Fail-open and transparency tests pass

**Ingest and data**

- [ ] Ingest schema uses `extra="forbid"`, and the limits, zip-bomb guard and invalid-key handling are tested
- [ ] Keys are hashed, shown once, and revocation works within 60 s
- [ ] The global baseline enforces P6 (k ≥ 5 **and** the dominance rule), with tests
- [ ] Deletion flows (project and account) tested end to end

**Web app**

- [ ] The IDOR suite covers every dashboard endpoint
- [ ] Security headers verified with an external scanner, e.g. Mozilla Observatory
- [ ] CSP has no `unsafe-inline` for scripts, and `dangerouslySetInnerHTML` is lint-forbidden
- [ ] OAuth `state` and PKCE in place, identity linking by subject, session cookie flags verified
- [ ] Rate limits on auth, API and ingest, and the Cloudflare WAF enabled

**Infrastructure and process**

- [ ] No secrets, keys or request bodies in logs, verified by an automated test on captured log output
- [ ] Database not publicly reachable, with least-privilege roles in use
- [ ] Backups enabled and a restore tested
- [ ] MFA on all accounts, registrar lock on
- [ ] PyPI Trusted Publishing and attestations set up, CI actions pinned to SHAs
- [ ] Dependency and container scans show no unresolved critical issues

**Policies and docs**

- [ ] Privacy policy, terms, `security.txt`, `SECURITY.md` and `PRIVACY.md` published

---

## 12. Selling points and positioning

### 12.1 Positioning statement

> For developers shipping features on top of AI model APIs, <Product> is reliability monitoring measured from **your own real traffic**. It tells you within seconds whether a slowdown or error is yours or the provider's, without your prompts or outputs ever leaving your app.
>
> Unlike LLM tracing tools that capture content to debug prompts, or status sites that ping endpoints, <Product> measures real workload performance and compares it, anonymously, with everyone else using the same model.

### 12.2 Taglines

- **"Is it you, or the model?"** (primary)
- "Two lines. Zero extra calls. Your prompts never leave your app."
- "Real-traffic reliability for every AI model you call."
- "Know before your users tell you." Use this **only after alerts exist** (paid), since the free tier has badges but no notifications.

### 12.3 Key selling points

1. **Answers "me or them?" in one view:** "whose side" error hints, the anonymised global baseline and official incidents together.
2. **Real workload data:** long inputs, tool use, streaming and the user's actual regions, not synthetic pings.
3. **Metadata only, by design:** easy to get through a security review.
   - The exact payload is published.
   - Debug mode lets users audit what is sent.
   - Sentinel tests prove nothing else leaks.
4. **Zero cost per call:** no extra tokens, and **no proxy in the request path**, so no added latency and no single point of failure.
5. **Fails open:** if we are down, their app doesn't notice.
6. **Every model, every route:** OpenAI, Anthropic, Google, Bedrock, Vertex, Azure and OpenAI-compatible providers in one view.
7. **Two-minute setup.**
8. **Open-source SDK:** anyone can audit what runs in their app.
9. **EU-hosted,** if decision D12 goes that way.
10. **A free tier that is useful on its own,** with the global baseline improving as more people join.

### 12.4 Competitive landscape

Verify each claim before using it in marketing. Describe categories fairly and never disparage.

| Category | Examples | What they generally focus on | How we differ |
|---|---|---|---|
| LLM tracing and observability | Langfuse, Helicone, LangSmith, Datadog LLM Observability, OpenLLMetry/Traceloop | Capturing prompts and responses, traces, cost, evals. Your own traffic only | Reliability view, metadata only by design, cross-user baseline |
| Status aggregators / "is it down?" | StatusGator, API Status Check, PulsAPI, official status pages | Scraping official status, endpoint pings | Real workload data, with partial degradation visible per model, route and input size |
| Performance leaderboards | Artificial Analysis, OpenRouter statistics | Their own test traffic, public comparisons | Your traffic, your regions, your workload |
| Gateways and routers | LiteLLM, OpenRouter, Portkey | Routing, fallbacks and key management, typically in the request path | No proxy. Works alongside them and underneath them |

### 12.5 Target users

1. **Indie developers and small startups** shipping chatbots, summarisers and agents. They want fast answers and a free tier.
2. **Agencies and freelancers** running AI features for clients. They need evidence that "it was the provider".
3. **Platform and SRE engineers at larger companies.** They need content-free telemetry that a security team will approve.
4. **European teams** that want EU-hosted, metadata-only tooling.

---

## 13. Marketing and launch plan

### 13.1 Principles

- **Honest claims only:**
  - No "we detect outages before status pages do" until it has been measured.
  - No fake testimonials and no inflated user numbers.
- **Lead with the problem and a live demo.**
- **Developer-first:** clear docs, copy-paste snippets and full transparency about data.

### 13.2 Assets to create

- **README hero GIF:** install, then the live dashboard within 60 seconds. Include the "what we send" JSON.
- **Landing page** (Section 10.1).
- **A 2-minute demo video.** Use the fake provider server to reproduce a realistic incident on demand.
- **Quickstart docs** and the `examples/` repo folder.

### 13.3 Phases

**1. Pre-launch (while building)**

- **Build in public:** weekly posts on LinkedIn and X about real technical decisions, for example "Why I measure TTFT at the first content delta, not the first byte". ADRs make good source material.
- **Job-search benefit:** this also shows recruiters real, shipped engineering.
- **Start collecting waitlist emails.**

**2. Private alpha**

- 5-10 developers: classmates, the DCU network, Discord communities.
- Watch them onboard and fix every point of friction.
- **Target:** at least 80% reach their first event within 5 minutes.

**3. Public launch (same week across channels)**

- **Show HN:** "Show HN: Open-source SDK that tells you if your LLM provider is slow, without seeing your prompts"
- **Reddit:** r/LLMDevs, r/LocalLLaMA, r/OpenAI, r/ClaudeAI, r/Python, r/SideProject. Follow each subreddit's self-promotion rules.
- **Product Hunt.**
- **A technical write-up** on dev.to or Hashnode.
- **Newsletters** such as Python Weekly, plus relevant Discord servers.

**4. After launch: the content engine**

- **Outage-time posts** with real aggregated charts that meet P6, e.g. "Model X slow in the EU right now: here's what N apps are seeing."
- **A monthly "State of LLM API reliability" report** from anonymised aggregates, once there is enough data and provider terms have been checked.
- **Technical deep dives:**
  - measuring TTFT without touching content
  - mergeable histograms
  - k-anonymity for shared baselines
  - fail-open SDK design
- **Integration guides:** LangChain, LlamaIndex, LiteLLM, FastAPI, Django, serverless. Submit PRs to framework integration lists and relevant "awesome" lists.
- **GitHub hygiene:** clear README, topics, releases, issue templates and fast responses.

### 13.4 Metrics to track

| Stage | Metrics |
|---|---|
| Reach | PyPI downloads, GitHub stars |
| Activation | Signups, activation rate (signup → first event within 24 h), docs → signup conversion |
| Retention | Weekly active projects, events per day, week-4 retention |
| Demand for paid | **Waitlist signups per paid feature** |

### 13.5 Sample copy

- **Short post:** "I built an open-source SDK that measures your AI model calls (latency, time to first token, errors) from your real traffic and tells you whether a slowdown is you or the provider. It never sees your prompts. Two lines to install."
- **Landing hero:** "Is it you, or the model? Real-traffic reliability monitoring for every AI model your app calls. Two lines of code. Zero extra calls. Your prompts never leave your app."

---

## 14. Roadmap and milestones

Build in this order. Each milestone must meet its "done when" criteria before moving on.

| # | Milestone | Scope | Done when |
|---|---|---|---|
| **M0** | Foundations | Monorepo, tooling, docker compose with Postgres, CI skeleton, ADR template, this brief and `CLAUDE.md` in the repo | `make dev` and `make test` work locally, and CI is green |
| **M1** | SDK core | `init`, config, call measurement model, per-minute summary aggregator (histograms, cardinality guard), sampling, bounded queue, sender, retry and backoff, fail-open guards, debug mode, `track()`, atexit and fork handling, local fake ingest | Sentinel and fail-open tests pass for `track()`, and the overhead benchmark runs in CI |
| **M2** | SDK integrations | `openai` (chat.completions, responses) and `anthropic` (messages.create, messages.stream), sync and async, streaming and non-streaming. Host and route detection. Fake provider server. Examples | Functional, sentinel and transparency tests pass across the compatibility matrix, and the examples produce correct events |
| **M3** | Ingest and storage | Ingest API, key hashing and cache, validation and limits, partitions, append-only inserts of summaries and samples, rollup worker merging summaries, retention job, Prometheus metrics, load test with results in `docs/capacity.md` (Section 20.6) | Example app → rows → correct rollups end to end. Malformed, oversized, zip-bomb and invalid-key tests pass. Load-test number recorded |
| **M4** | Web app core | OAuth, sessions, projects, keys, onboarding with first-event check, overview, live feed, model detail, badges, hints, settings, deletion | A new user goes from signup to first event on the dashboard in under 5 minutes. IDOR suite passes. Headers verified |
| **M5** | Official status | Poller adapters, component mapping, banner, chart shading, incidents page | At least OpenAI, Anthropic, Google, AWS and Azure sources ingesting, with incidents visible on the right charts |
| **M6** | Global baseline | 5-minute and 1-hour global rollups, P6 enforcement, overlay and vs-everyone indicator, sharing toggle | k and dominance tests pass, and the toggle excludes a project from future aggregates |
| **M7** | Hardening and launch | Section 11.11 checklist, Terraform for production, CD to production, own observability, docs, landing and legal pages, PyPI 0.1.0, private alpha | Every checklist item ticked and alpha users onboarded successfully |

**Later (still free tier)**

- `google-genai` and Bedrock (`boto3`) integrations, and embeddings.
- TypeScript SDK and OTLP ingest.
- SSE live feed and a serverless helper.
- Rate-limit header capture.
- Public status page and the monthly report.

**Paid (Section 8):** only after the waitlist shows real demand.

---

## 15. Repo structure and conventions

```
/
├── PROJECT_BRIEF.md            # this file (source of truth)
├── CLAUDE.md                   # short agent guide (Appendix C)
├── README.md  SECURITY.md  PRIVACY.md  LICENSE
├── sdk/python/                 # yourpkg: src/yourpkg/, tests/, noxfile.py, pyproject.toml
├── server/                     # FastAPI: app/ingest, app/api, app/auth, app/core, migrations/, tests/
├── worker/                     # rollups, retention, status poller (may share code with server/)
├── web/                        # React + TypeScript + Vite
├── tools/fake-provider/        # simulated AI provider for dev, tests and demos
├── examples/                   # sample apps using the SDK
├── infra/terraform/            # Cloudflare, database, cloud resources
├── docs/                       # user docs; docs/decisions/ holds ADRs
└── .github/workflows/
```

**Conventions**

- **Python:** 3.12 for the server, with ruff and mypy.
- **TypeScript:** strict mode.
- **Commits and PRs:** conventional commits, and small PRs that include tests. Update docs and ADRs whenever behaviour changes.
- **Code rules:**
  - No secrets in code. Alembic is the only way to change the schema.
  - Plan differences go through `entitlements()`.
  - All times are in UTC, and durations are in ms.

---

## 16. Open decisions

Ask Saad about each of these. Use the default only if he says so.

| # | Decision | Options | Default |
|---|---|---|---|
| D1 | Product, domain and package name | n/a | Check PyPI, npm, the domain, a GitHub org and trademark conflicts before choosing |
| D2 | Hosting | **Fly.io:** familiar, fastest to ship. **AWS:** ECS Fargate or App Runner, or Lambda + API Gateway for ingest, plus RDS. Fills the AWS CV gap but takes more setup | Fly.io for the MVP, with Terraform for Cloudflare and the database. Revisit AWS for ingest as a later learning extension. **If AWS experience matters more than speed, choose AWS now** |
| D3 | Database layout | Plain Postgres partitioning plus our own rollups, or TimescaleDB | Plain Postgres: portable and teaches more. Keep the repository layer |
| D4 | Frontend | React + Vite SPA, or Next.js | React + Vite SPA, with FastAPI handling auth |
| D5 | Licences | SDK: Apache-2.0 or MIT. Server: closed, AGPL or source-available | SDK Apache-2.0. Server repo private until launch, decide then |
| D6 | Global sharing default | On with clear disclosure and opt-out, or opt-in | On by default, explained during onboarding, one-click opt-out |
| D7 | Free-tier limits | n/a | 1M calls measured/month per account, 7-day retention, 3 projects |
| D8 | Wire format: per-call events or client-side aggregation | Every call sent individually, or per-minute summaries plus errors and a sample | **Summaries plus errors and a sample (Section 19).** Decide before the first SDK release, because the wire format is the hardest thing to change once old SDK versions are installed in users' apps |
| D9 | Live feed transport | Polling or SSE | Polling every 3-5 s for the MVP |
| D10 | Postgres Row-Level Security | Add RLS, or app-level scoping only | App-level scoping plus IDOR tests in the MVP. Add RLS in M7 if time allows |
| D11 | Minimum Python version | 3.9, 3.10, 3.11 | 3.10+ |
| D12 | Hosting region | London, Frankfurt, Amsterdam | **Frankfurt or Amsterdam**, so "EU-hosted" is literally true. London is outside the EU, though the UK has an EU adequacy decision |

---

## 17. Facts to verify before relying on them

These were true to the best of our knowledge on 1 October 2026, or are reasonable assumptions, but they change. Check current official docs while building.

**Provider SDKs**

- Current module paths and class and method names in the `openai` and `anthropic` Python SDKs:
  - chat completions, responses, messages and the `messages.stream` helper
  - the `AzureOpenAI`, `AnthropicBedrock` and `AnthropicVertex` clients
  - stream classes, and how and when usage appears in streams
- Default retry counts and timeouts in each provider SDK.
- That OpenAI's `stream_options.include_usage` adds a final chunk with empty `choices`.

**Errors, headers and hosts**

- Error codes: OpenAI's `insufficient_quota` (returned with 429), Anthropic's `overloaded_error` (529), and others.
- Rate-limit header names (OpenAI `x-ratelimit-*`, Anthropic `anthropic-ratelimit-*`), once we capture them.
- Provider host patterns: Azure domains, Bedrock runtime hosts, Vertex regional hosts, the Gemini API host and OpenAI-compatible providers.

**Status pages and standards**

- Each provider's status page URL and machine-readable format, including which ones use Atlassian Statuspage.
- The OpenTelemetry GenAI semantic conventions: current attribute names and stability status (they are still evolving), and **which attributes and events carry content and must be stripped**.

**Infrastructure**

- Managed Postgres support for TimescaleDB features and for pg_partman.
- Fly.io regions and Terraform support, or the AWS equivalents.
- PyPI Trusted Publishing and attestation setup steps.
- Which Python versions the current provider SDKs support.

**Legal and market**

- AI provider terms on publishing performance data.
- GDPR obligations. Get proper advice before charging.
- Competitor features, before writing any comparison copy.

---

## 18. Interview narrative

Build with this in mind from day one.

**30-second pitch** (fill in real numbers once measured):

> "I built an open-source Python SDK and hosted service that monitors the reliability of AI model APIs from an app's real traffic. It wraps OpenAI and Anthropic calls, records only metadata like time to first token and error class, and ships it in batches to a FastAPI ingest service on Postgres. The dashboard shows per-model health, official incidents and an anonymised cross-user baseline, so developers can tell whether a slowdown is theirs or the provider's. It never sees prompts and adds about X microseconds per call."

**Deep-dive topics to be ready for**

- **SDK design:**
  - Fail-open design: bounded queue, background thread, never raising.
  - Measuring TTFT correctly for streams without changing user-visible behaviour.
  - Method-level vs transport-level instrumentation, and why.
- **Backend and data:**
  - Write-only hashed ingest keys.
  - Tenant isolation and IDOR testing.
  - Why percentiles can't be averaged, and how mergeable histograms solve it.
  - Client-side aggregation (summaries plus sampling), and why the wire format was fixed before release.
  - Why the pipeline is shared and multi-tenant rather than workers per user, and the scaling stages (Section 20).
  - k-anonymity and the dominance rule for the shared baseline.
  - Partitioned tables and retention.
  - Worker coordination with advisory locks or `SKIP LOCKED`.
- **Delivery and evidence:**
  - Load-test results.
  - Terraform, GitHub Actions and Trusted Publishing.
  - Privacy sentinel tests.
  - Any incident and post-mortem from running it.

**Rule:** quote only numbers that were actually measured.

**STAR stories to capture during the build**

- A bug caught by the sentinel or fail-open tests.
- A performance regression caught by the benchmark.
- A design decision reversed and recorded in an ADR.
- Something that broke in production, and how it was fixed.

---

## 19. Data pipeline: what actually happens (summaries and samples)

This section defines the **wire format**, meaning what the SDK sends. It must be settled before the first SDK release, because old SDK versions keep running in users' apps for months or years and the server has to keep accepting their format.

### 19.1 Plain-English walkthrough

**Analogy:** a shop with several tills. Instead of photocopying every receipt to head office, each till sends an end-of-hour total, plus copies of any refunds or problems and a few random receipts for spot checks. Head office adds the tills together.

**Example.** The user's `summariser` app runs on 2 servers with 4 worker processes each, so **8 copies of the SDK** are running. Between 10:00 and 10:01, process #3 makes 120 streaming calls to `claude-sonnet-x` with 1k-10k-token inputs.

1. **Every call is tallied.** The wrapper times the call and adds it to an in-memory tally for that **series** (model, route, streaming, input bucket, etc.) and minute:
   - count +1, the status and the token counts
   - +1 in the right histogram bucket for TTFT, duration and decode rate

   This is a few dictionary and array updates, a few microseconds of work.
2. **Errors and samples are sent straight away.** These are queued as **sample records** and sent within ~5 s, which powers the live feed:
   - the 2 calls that failed with `overloaded`
   - the first call of this series in this process
   - about 1 in 20 successful calls, capped at 10 per minute
3. **At about 10:01:02, the tally is closed.** The 10:00 tally is sent as **one summary record**:
   - count 120: 117 ok, 1 cancelled, 2 `overloaded`
   - the TTFT histogram: 1 call in the 200-300 ms bucket, 3 in 300-500 ms, and so on
   - input and output token totals
4. **The server stores it.** The summary is appended to `summary_records`. The other 7 processes send their own summaries for 10:00.
5. **The rollup worker adds them up.** All 8 summaries become one `rollups_1m` row for that project, series and minute. This works because every histogram uses the same fixed buckets: **percentiles cannot be added together, but bucket counts can.**
6. **The dashboard reads both.** Rollups feed the charts, badges and global baseline. Samples feed the live feed, recent errors and onboarding.

**Result:** process #3's 120 calls became **1 summary record and about 9 sample records** instead of 120 records.

### 19.2 Summary record

```json
{
  "summary_id": "4f1c0a7e-8a2b-4d6e-9c3f-2b7d9e1a5c60",
  "minute": 1790000040000,
  "series": {
    "provider": "anthropic",
    "route": "direct",
    "endpoint_host": "api.anthropic.com",
    "endpoint_region": null,
    "operation": "messages",
    "model": "claude-sonnet-x-20260801",
    "streaming": true,
    "input_bucket": "s",
    "service": "summariser",
    "environment": "prod",
    "origin_region": "fra"
  },
  "count": 120,
  "ok": 117,
  "cancelled": 1,
  "errors": { "overloaded": 2 },
  "ttft_hist":     [0,0,0,0,1,3,12,40,59,3,0,0,0,0,0,0,0,0,0,0],
  "duration_hist": [0,0,0,0,0,0,0,0,0,0,2,9,51,44,12,2,0,0,0,0],
  "decode_hist":   [0,0,0,0,1,6,48,55,7,0,0,0,0,0],
  "input_tokens_sum": 512340,
  "output_tokens_sum": 48211,
  "output_tokens_estimated_count": 0
}
```

**Notes on the fields**

- **`minute`** is the start of the minute (epoch ms, UTC). Calls are counted in the minute they **finish**, which keeps flushing simple even for streams that run for several minutes.
- **`model`** is the model returned by the provider, or the requested model if none was returned.
- **Histograms** use the fixed bucket boundaries in Section 9.4, giving 20 latency buckets and 14 decode-rate buckets.
  - A call is counted only in histograms that apply to it. For example, calls that fail before the first token have no TTFT.
  - The boundaries are part of the protocol. Changing them means a new `schema_version`, and the server must support both versions.
- **The envelope** (Section 6.4) carries both arrays: `"summaries": [...]` and `"samples": [...]`.

### 19.3 SDK rules

**Aggregator**

- A dict keyed by `(minute, series)` holding counters and histogram arrays.
- O(1) update per call under a short lock.
- Closed minutes are flushed about 2 s after the minute ends.

**Samples**

| Rule | Detail |
|---|---|
| Always send errors | Capped at 100 per minute per process. Errors beyond the cap are only counted in the summary |
| Always send the first call of each new series per process | Lets onboarding confirm the setup within seconds, and makes new models appear immediately |
| Random successful calls | `sample_rate` (default 0.05, i.e. 1 in 20), capped by `max_samples_per_minute` (default 10 per process) |

**Cardinality guard**

- At most **500 active series per process per minute.** Beyond that, extra series are folded into `model="other"`, the overflow is counted, and a warning is logged once.
- This stops memory and storage blowing up if someone passes dynamic values, such as user IDs, as tags.

**Lifecycle**

- **Exit:** flush the current partial minute as well as queued samples.
- **Fork:** the child process resets its aggregator.

**Kept unchanged:** the bounded queue, background thread, fail-open behaviour and `debug` mode, which prints both summaries and samples.

### 19.4 Server rules

**Ingest is append-only.**

- Summaries go into `summary_records` and samples into `call_samples`, both with idempotent IDs.
- **No updates on the hot path.** If 8 processes report the same minute, that is 8 inserts, not 8 updates competing for one row.

**The worker merges.** It recomputes `rollups_1m` by summing summaries per project, series and minute over the last 15 minutes (Section 9.5). Then it builds the coarser grains and the global baseline.

- Postgres has no built-in element-wise array sum. Options:
  - a small custom aggregate
  - `unnest ... WITH ORDINALITY` with `GROUP BY`
  - summing in the worker's Python code

  Pick the simplest that performs well in the load test, and record it in an ADR.

**Quotas:** user-facing limits count **calls measured** (the sum of `count`). Protective limits also apply to records and requests.

### 19.5 Volume comparison (estimates)

| App traffic | Records if every call were sent | Records with summaries and samples | Reduction |
|---|---|---|---|
| 1,000 calls/day, 1 model, 1 process, bursty (~200 active minutes) | 1,000 | ~250 | ~4× |
| 100k calls/day, 2 models, 2 processes, steady | 100,000 | ~5.8k summaries + ~5k samples ≈ 11k | ~9× |
| 10M calls/day, 2 models, 8 processes, steady | 10,000,000 | ~23k summaries + ~115k samples (capped) ≈ 140k | ~70× |

**Takeaway:** the cost per app grows slowly with its traffic, so heavy users stay cheap. Small apps see little reduction, but they are cheap anyway.

### 19.6 Trade-offs (be honest about them in the docs)

- **The live feed is sampled.** It shows every error (up to the cap) but only a sample of successful calls. Label it clearly.
- **Percentiles are approximate.** They are interpolated within histogram buckets, so error is bounded by bucket width. Document the boundaries.
- **Charts lag by about 1-2 minutes,** because summaries flush once a minute. The live feed lags by about 5 seconds.
- **Raw per-call history is not kept** beyond the samples. That is less to store, and a privacy plus.

---

## 20. Scaling architecture and costs

### 20.1 Principles

- **One shared, multi-tenant pipeline. There are no workers or machines per user.** Every record carries its project (from the key), and the same small set of services handles everyone.
- **The measuring runs in the user's app, on their machine and at their cost.** Our servers only receive small records, which is microseconds of work each.
- **Our cost grows with records stored and processed, not with the number of users.** Section 19 keeps records per app roughly flat as the app's traffic grows.

### 20.2 Why not a job queue with workers (as in PDF-to-Video)

| | PDF-to-Video | This project |
|---|---|---|
| Unit of work | A heavy job: minutes of CPU, large files | A tiny record: ~0.5-1 KB, microseconds |
| Right pattern | Job queue (`SKIP LOCKED`) with workers that scale with jobs | **Streaming data pipeline:** batch, append and aggregate |
| What scales cost | Number of jobs | Records per second and data retained |

The `SKIP LOCKED` experience still applies, for coordinating the rollup and retention jobs.

### 20.3 Growth stages and when to move

**Stage 1: launch (build this)**

```
SDKs ──► Cloudflare ──► ingest ×2 (stateless) ──► Postgres (append-only, partitioned)
                                                      ▲
                               worker ×1 (rollups, global baseline, retention, status poller)
Browsers ──► Cloudflare ──► web-api ×1-2 ──► reads rollups and samples
```

**Move to stage 2 when any of these persist** (read them from our own metrics, Section 9.10):

- database CPU above ~60-70%
- insert p95 latency climbing
- rollup lag above 2 minutes
- a need to keep accepting data during database maintenance or outages

**Stage 2: add a buffer between ingest and the database**

- Ingest writes to a stream and acknowledges straight away. A consumer group then batch-writes to the database.
- **Stream options:**
  - **Redis Streams** (e.g. Upstash): simplest, and Saad already knows Redis
  - **NATS JetStream**
  - **Kafka / Redpanda:** heavier, the industry standard at large scale
  - **AWS Kinesis or SQS**, if hosted on AWS
- **Benefits:** spikes are absorbed, a database outage means data waits instead of being lost, and consumers scale independently.
- **Why not Postgres as the queue:** a Postgres `SKIP LOCKED` queue would put the buffer on the very database that is the bottleneck. This is a good interview point.

**Stage 3: a columnar analytics store**

- Move `summary_records`, `call_samples` and rollups to **ClickHouse**, which compresses well, makes time-range queries fast, and whose materialised views can replace much of the rollup worker. Postgres stays for accounts, projects, keys and status data.
- This is the pattern used by large observability products. Sentry and PostHog, for example, use Kafka feeding ClickHouse.
- **Move when** rollup queries slow down, storage cost dominates, or volume reaches tens of millions of records a day.

**Stage 4: regional ingest (probably never needed early)**

- Ingest machines in several regions, all writing to the central stream.
- The SDK sends in the background, so latency to ingest barely matters. Only do this for data-residency demands.

**Alternative path: serverless ingest**

- Cloudflare Workers or AWS Lambda, each feeding a queue. You pay per request and scale to zero, which is attractive while traffic is low and spiky.
- The cost is more vendor-specific code. Consider it if ingest machines become a meaningful share of cost.

**Scaling the read side**

- Dashboards read pre-computed rollups, so reads are cheap.
- If needed: a read replica, a 15-30 s cache of overview responses per project, and Server-Sent Events fan-out via Redis pub/sub.

### 20.4 Capacity math

These are estimates. Replace them with load-test results (20.6).

- **Requests:** each app process sends about one request every 5 s, about 0.2 requests per second.
  - 1,000 active app processes ≈ 200 req/s.
  - 10,000 ≈ 2,000 req/s, which a handful of small async FastAPI instances behind Cloudflare should handle.
- **Records:** 1,000 apps averaging 100k calls a day produce about 11M records a day, roughly 130 per second on average. That is comfortable for a single Postgres database using batched `COPY` inserts.
- **Storage:** at ~0.6 KB per record, 11M records a day is about 6.6 GB a day. With 7-day retention, that is ~45 GB plus rollups and indexes. That is fine for managed Postgres. ClickHouse would typically compress it 5-10×.
- **Realistic free-tier averages will be far lower** than 100k calls per app per day.

### 20.5 Cost expectations

These are rough figures. **Verify current pricing before relying on them.**

| Stage | Components | Rough monthly cost |
|---|---|---|
| Launch | 2 small ingest machines, 1 web-api, 1 worker, small managed Postgres, Cloudflare (free tier), Grafana Cloud (free tier) | Tens of dollars, roughly $30-100 |
| Stage 2 | Adds a managed Redis or stream service | Roughly +$10-50 |
| Stage 3 | ClickHouse (managed or self-hosted) | Roughly $100+ |

Track **cost per 1,000 calls measured** every month (Section 21) so pricing for the paid tier is based on real numbers.

### 20.6 Load-test plan (part of M3, rerun before each stage change)

**Traffic generator:** simulates many SDK processes sending realistic envelopes, with summaries every minute and samples every ~5 s.

**Scenarios:**

1. steady load
2. a sudden 10× spike
3. a slow or unavailable database
4. a flood of invalid keys
5. oversized and malformed payloads

**Measure:**

- maximum sustained requests/s and records/s with ingest p95 under 200 ms
- database CPU and I/O
- rollup lag
- error rates

**Record** the results in `docs/capacity.md`, including the machine sizes used. These become real interview numbers.

### 20.7 Design seams to build now

These keep later stages from needing rewrites. Build only these seams; no other speculative abstraction.

- **A `Sink` interface in ingest:** `PostgresSink` now, `StreamSink` in stage 2.
- **A repository layer for metric queries:** Postgres now, ClickHouse in stage 3.
- **A versioned wire protocol (`schema_version`):** the server supports the current and previous versions.
- **Stateless ingest and web-api:** sessions and state live in the database, never in local memory or on disk.
- **Limits come from `entitlements()`,** not constants.
- **Idempotent IDs on every record,** so retries and replays from a buffer are safe.

---

## 21. Efficiency principles (no bloat)

Efficiency is a core requirement. Every addition must earn its place.

**Architecture**

- Do the simplest thing that meets the requirement. Add a component only when a **measured** trigger (Section 20.3) says so.
- No Kafka, Kubernetes or microservices "just in case".
- **Few deployables:** `ingest`, `web-api`, `worker`, and the static web app. Any new service needs an ADR.

**Data**

- **Send less:** summaries plus samples (Section 19).
- **Store less:** retention by dropping partitions.
- **Compute once:** rollups.
- **Read pre-aggregated data:** never scan raw records to render a chart.

**Hot paths**

- **Ingest:** append-only and batched with `COPY`. No per-record database round trips, no ORM on the ingest path (use `asyncpg`), and no synchronous calls to external services.
- **SDK:** O(1) work per call, minimal allocations, nothing on the caller's thread except the tally update.

**Dependencies**

- The SDK has zero runtime dependencies.
- In the server and web app, justify every new dependency in the PR. Prefer the standard library and platform features, and remove unused packages.

**Frontend**

- An initial JavaScript budget of about **200 KB gzipped**.
- One lightweight charting library and no component-library sprawl.
- Lazy-load detail pages.

**Infrastructure**

- Use the smallest machine sizes that meet the internal targets.
- Scale out, not up, and autoscale down to the minimum.

**Code**

- No speculative abstractions beyond the seams in Section 20.7.
- Delete dead code.
- Measure before optimising: benchmarks and load tests are the source of truth.

**Cost awareness**

Track monthly:

- records per day
- storage size
- **cost per 1,000 calls measured**

---

## Appendix A: Glossary

| Term | Meaning |
|---|---|
| **TTFT** | Time to first token: call start → first output chunk of a stream |
| **Decode rate** | Output tokens per second after the first token |
| **p50 / p95** | Median and 95th percentile: half of calls, and 95% of calls, are at or below this value |
| **Route** | How a model is reached: direct API, Azure, Bedrock, Vertex, OpenAI-compatible, custom, local |
| **Input bucket** | Input-size group (`xs` < 1k, `s` 1k-10k, `l` ≥ 10k tokens) used for fair comparison |
| **Rollup** | Server-side per-minute (or coarser) totals, built by adding up summary records |
| **Summary record** | One SDK process's tally for one series over one minute: counts, error counts, histograms, token sums |
| **Sample record** | One individual call sent in full (metadata only): every error, the first call of each series and a small random share |
| **Series** | One combination of provider, route, model, streaming, input bucket, service, environment and origin region |
| **Cardinality guard** | Cap on how many distinct series one process tracks, so memory and storage cannot blow up |
| **Mergeable histogram** | Fixed-bucket counts that can be summed across time and projects to compute percentiles |
| **k-anonymity (here)** | Show a global data point only if at least k (5) distinct projects contributed |
| **Dominance rule** | Hide a point if one project contributes more than 50% of its samples |
| **Fail open** | If monitoring fails, the monitored app carries on unaffected |
| **IDOR** | Insecure direct object reference: reading another tenant's data by changing an ID |
| **CSP / HSTS** | Browser security headers restricting script sources, and forcing HTTPS |
| **PKCE** | OAuth extension that protects the authorisation-code flow from interception |
| **OTLP / OTel GenAI conventions** | OpenTelemetry's protocol, and its standard attribute names for AI model calls |
| **SSE** | Server-Sent Events: one-way live updates from server to browser |
| **Trusted Publishing** | Publishing to PyPI from CI using OIDC identity instead of stored tokens |
| **ADR** | Architecture Decision Record: a short write-up of one design decision |

---

## Appendix B: Sources (research gathered 1 October 2026)

- TierZero, "Your AI Provider Had 5 Outages Last Month": https://www.tierzero.ai/blog/ai-provider-outages-2026/
- API Status Check, Anthropic status and history: https://apistatuscheck.com/api/anthropic
- Nordic APIs, "API Reliability Report 2026": https://nordicapis.com/api-reliability-report-2026-uptime-patterns-across-215-services/
- "An Empirical Characterization of Outages and Incidents in Public Services for Large Language Models": https://arxiv.org/html/2501.12469v2
- StatusGator (Anthropic API): https://statusgator.com/services/anthropic/claude-api-apianthropiccom
- PulsAPI (Claude): https://www.pulsapi.com/services/claude
- Maxim Bifrost provider status: https://www.getmaxim.ai/bifrost/provider-status/anthropic
- OpenTelemetry GenAI semantic conventions guide: https://hidekazu-konishi.com/entry/opentelemetry_genai_semantic_conventions_guide.html
- Langfuse OpenTelemetry integration: https://langfuse.com/integrations/native/opentelemetry
- Open-source AI observability tools list: https://awesomeosai.com/alternatives/ai-observability-tools

---

## Appendix C: Suggested `CLAUDE.md` (keep it short; it loads every session)

```markdown
# <Product>: agent guide

Read PROJECT_BRIEF.md before any non-trivial work. It is the source of truth.

## Scope
- Build the FREE tier only (brief §7). Paid features (§8) are parked.
- Ask Saad before resolving any open decision (§16).

## Non-negotiables
- Privacy invariants P1-P7 (§5): the SDK never captures prompts, outputs, keys,
  URL paths/queries or error message text, never makes extra calls, never
  modifies requests/responses, and always fails open.
- Security requirements (§11). Never log keys, Authorization headers or request bodies.
- Efficiency first, no bloat (§21). No new components, dependencies or abstractions
  without a measured need. The wire format is defined in §19.

## Working style
- Small PRs with tests. Run `make lint test` before finishing.
- Record non-trivial choices as ADRs in docs/decisions/.
- Verify SDK internals and external formats against current docs (§17).
- Keep responses concise. Saad will ask for detail.

## Commands
make dev · make test · make lint
```
