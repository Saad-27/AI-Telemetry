# Session handoff

Read this at the start of every session, after `CLAUDE.md`. Update it at the end of every
session, in the same PR as the session's other changes, so it reaches `main`.

**Last updated:** 2026-10-17 (M2 done; M3 step 1, the schema, in a PR)

## Where the project is

- **Done: M0 and M1.** The `gaslit` Python SDK (`sdk/python/`) has `init`, `track`, `flush`,
  `shutdown`, per-minute summaries, sampling, the bounded queue, the background sender, fork and
  exit handling, and debug mode. Privacy, fail-open tests and the overhead benchmark run in CI.
- **Done: M2.** OpenAI and Anthropic integrations, host detection, fake provider, examples
  (PRs #13-18). Notes kept below under "M2 notes".
- **Server** (`server/`): FastAPI with `/healthz` and `/readyz`. Schema and Alembic in review
  (M3 step 1). No ingest yet.
- **Deployment:** written but switched off (repo variables `PRODUCTION_ENABLED`, `INFRA_ENABLED`
  unset). Production only, no staging (ADR 0006). Nothing is hosted or paid for until M3 (ADR 0007).
- **In progress: M3** (ingest and storage), plan below. Step 1 is on branch `m3-schema`,
  waiting for Saad's review. Hosting is the last step; confirm costs with Saad first.

## Decisions made recently (details in the ADRs)

| ADR | Decision |
|---|---|
| 0005 | Product name **Gaslit**, `gaslit.dev`. Deploy by CI after tests pass; Terraform manages Cloudflare |
| 0006 | Production only, no staging. Each deploy needs Saad's approval in GitHub |
| 0007 | Database on **Neon** (Free while testing, Frankfurt). One 256 MB Fly machine that scales to zero. No hosting before M3 |
| 0008 | **One shared database** for all users, with per-account limits via `entitlements()` (no per-user databases). **No rollup worker at launch**: the dashboard sums summaries at read time. Includes the multi-tenant checklist for M3 |
| 0009 | Host detection rules the brief left open: only the hostname is read, unknown Bedrock vendor is `custom`, Vertex global region is `global`, "local" means not globally routable |
| 0010 | OpenAI integration: wrap `create` on the resource classes, measure streams by swapping their inner iterator, dropped streams are `cancelled`, `openai` 1.30+ supported. Azure deployment names are hidden (confirmed) |
| 0012 | Examples run offline: the fake provider acts as an HTTP proxy so clients keep real hostnames (`http://` URLs). The fake also serves Azure, Bedrock `invoke` and Vertex paths |
| 0013 | First schema (proposed, in review): day partitions made by `app.db.partitions`, `NOLOGIN` roles `ingest_writer`/`dashboard_app` with RLS on data tables, ingest batches with `INSERT ... unnest` because `COPY` is refused under RLS, one column per error class, Alembic over asyncpg |
| 0011 | Anthropic integration: same approach, shared code in `_wrap.py`; `messages.stream()` measured by wrapping the manager's enter; in-stream `error` events classified by type; input tokens include the prompt cache; Bedrock ARNs reduced to the public model id or `bedrock-arn` (confirmed); `anthropic` 0.28+ supported |

## Platform status (outside the repo)

| Item | Status |
|---|---|
| Domain `gaslit.dev` | Bought on Cloudflare Registrar 2026-10-14. DNSSEC was "pending" (Cloudflare adds the DS record itself) |
| Email-spoofing records (null MX, SPF `-all`, DMARC reject) | Done |
| PyPI name `gaslit` | Claimed: placeholder 0.0.0 uploaded 2026-10-14. The upload token was deleted |
| GitHub branch protection on `main` | Done |
| GitHub secret scanning | Saad chose not to enable it |
| Repo visibility | **Public**, which keeps environment approval gates free (ADR 0006). D5 said private; Saad has not changed it |
| Neon, Fly app, Cloudflare tokens, GitHub environments, Terraform, certificates | **Not started.** Runbook steps 2-6 in `docs/deploy.md`, at M3 |
| Google and GitHub OAuth apps | M4 (runbook step 7) |

## Next: M3 plan (small PRs, in order)

1. **Schema: IN REVIEW** (`server/migrations/`, `server/app/db/partitions.py`, ADR 0013).
   `make migrate` = `alembic upgrade head` + this week's partitions. Tests (`-m db`) run in a
   fresh `gaslit_test` database next to `DATABASE_URL`'s and cover partitions, idempotent
   inserts, role grants and RLS.
2. **Ingest keys:** `gl_live_` + base62 + checksum (brief §11.4), SHA-256 lookup, 60 s cache.
3. **`POST /v1/ingest`:** size caps with a streaming gunzip, Pydantic validation of the §19
   wire format, server fields, `INSERT ... SELECT FROM unnest() ON CONFLICT DO NOTHING`
   behind the `Sink` seam (§20.7).
4. **Rate limit and quota** from `entitlements()`, `429` with `Retry-After`.
5. **Retention job** (drop old partitions, and decide how `ensure_partitions` runs in
   production) and **Prometheus metrics**.
6. **Read-time sums** (repository layer) and the end-to-end test: example app → rows → sums.
7. **Load test**, results in `docs/capacity.md`.
8. **Hosting** (runbook steps 2-6), with login roles created by SQL and granted
   `ingest_writer`/`dashboard_app`. Confirm costs with Saad first.

## M2 notes (done)

1. **Fake provider server: DONE** (`tools/fake-provider/fake_provider.py`, stdlib only). Serves
   `/v1/chat/completions`, `/v1/responses` and `/v1/messages`, streaming or not. Control it per
   call with a `Behavior` (`status`, `latency_ms`, `chunk_ms`, `chunks`, `retry_after_s`, and for
   privacy tests `word` (the reply text) and `echo` (errors quote the request)), the
   `x-fake-behavior` header, or `FakeProvider.script(...)` in tests. A timeout is a `latency_ms`
   above the client's timeout. Checked against the real `openai` and `anthropic` clients (ad hoc,
   not a repo dependency); `sdk/python/tests/test_fake_provider.py` covers it with plain HTTP.
   Not built yet (add when a test needs it): a stream cut off mid-way, a disconnect counter, Azure/
   Bedrock/Vertex paths. Real clients retry 429/5xx by default, so tests use `max_retries=0`.
2. **Host and route detection: DONE** (`sdk/python/src/gaslit/_hosts.py`, ADR 0009).
   `detect(base_url, model)` returns `Endpoint(provider, route, host, region)`; it reads only the
   hostname and never raises. Called on every request, behind two small caches (URL to host,
   host to endpoint). Server-side re-validation of the allowlist is M3.
3. **OpenAI integration: DONE** (`sdk/python/src/gaslit/_openai.py`, ADR 0010). `init()` now
   patches `openai` (`instrument=` param). Tested on openai 1.30.0, 1.66.0, 2.0.0 and latest
   (3.31.0); the `sdk-compat` CI job runs the older three. Overhead (benchmark): ~10-15 µs per
   call, under 1 µs per streamed item. Streams dropped unfinished are handed to a lock-free,
   bounded queue in `_runtime` (`abandon()`), because a garbage-collection finalizer may run
   while an SDK lock is held; each later call records at most 32 of them and `flush()` takes the
   rest. Confirmed by Saad: on Azure the `model` argument is the customer's deployment name, so
   `model_requested` is reported as `azure-deployment` (ADR 0010 point 5). Not covered yet:
   `with_raw_response`/`with_streaming_response` (status and duration only), `parse()` and
   `stream()` helpers that bypass `create`, embeddings.
4. **Anthropic integration: IN REVIEW** (`sdk/python/src/gaslit/_anthropic.py`, ADR 0011).
   `messages.create` (sync/async, streaming or not) and the `messages.stream()` helper; the
   Bedrock, Vertex and Foundry clients share the same classes. The shared wrapper and stream code
   moved from `_openai.py` to `_wrap.py`. Tested on anthropic 0.28.0, 0.50.0, 0.124.0, 1.0.0 and
   1.15.0 (latest); the `sdk-compat` CI job now runs both libraries (older ones need
   `httpx<0.28`). Overhead: ~13 µs per call, ~0.4 µs per streamed event. The fake provider
   gained `cached_tokens` and `stream_error` (an Anthropic `error` event after HTTP 200).
   Saad confirmed the Azure and Bedrock ARN rules. Not covered, by choice for now: `beta.messages`
   (and the tool runner), `messages.parse()`. Saad is fine with the newer Bedrock Mantle,
   `AnthropicAWS` and `AnthropicGoogleCloud` hosts staying `custom`.
5. **Examples: IN REVIEW** (`examples/`, ADR 0012). `openai_chat.py`, `anthropic_summariser.py`,
   `cloud_hosts.py` (Azure OpenAI, Bedrock, Vertex) and `manual_track.py`, each printing its
   payload in debug mode with no key. `_demo.offline()` starts the fake provider and sets
   `HTTP_PROXY`, so `http://api.openai.com/v1` and the like reach it with the real hostname.
   The fake provider now routes by path suffix (Azure deployment paths, Bedrock `invoke`, Vertex
   `rawPredict`/`streamRawPredict`, model from the path) and has a `model` behavior (the model
   a reply reports). `tests/test_examples.py` runs each one and checks the events and that no
   prompt, reply, exception text or customer name leaks; it needs the latest libraries, so the
   compat jobs skip it. Examples are ruff-linted (Makefile, CI), not mypy-checked. Not served
   yet: Bedrock streaming (AWS binary event stream).

Never capture prompts, outputs, keys or error message text (P1-P7). Read installed library
source, not memory, before relying on it (brief §17).

**Test patterns (both integrations):**
- Real client against the fake provider with `max_retries=0`: `tests/test_openai*.py`,
  `tests/test_anthropic*.py`. Each has the matrix, error classes, cancellation, transparency,
  fault injection and privacy sentinels.
- Gotchas: cancelled calls are counted but only the first of a series is sampled, so read them
  from the summaries. Fault injection must patch the name where it is used
  (`gaslit._anthropic.detect`, not `gaslit._hosts.detect`), and a function that guards itself
  (like `_take_call`) must be broken from inside. Each fault test asserts the fault was reached.
- Older versions: `uv run --with "anthropic==X" --with "httpx<0.28" pytest ...`.

## Loose ends

- **Security config:** `docs/security-config.md` (PR #28) lists every setting that keeps users'
  data safe, with status. Check it at each step. Open gap for step 8: requests to
  `*.fly.dev` skip Cloudflare; proposed fix is a secret header added by Cloudflare.

- `sdk/python/src/gaslit/_config.py`: `DEFAULT_ENDPOINT` is still `https://ingest.gaslit.invalid`
  with a stale "until the name is chosen" comment. Change it to `https://ingest.gaslit.dev`
  before the first real release (0.1.0, M7).
- Neon connection strings include `channel_binding=require`; asyncpg forwards it to Postgres,
  which rejects it. Remove it from `DATABASE_URL` (runbook step 2).
- `make test` needs Docker for the server's database tests. Cloud sessions have no Docker: run
  `uv run pytest` in `sdk/python`, and in `server` with `-m "not db"`.
- The app still connects as the schema owner, which bypasses RLS. Separate login roles for
  ingest and the dashboard come with hosting (M3 step 8) and M4 (ADR 0013).

## Open discussions (not decided; raise with Saad when relevant)

- **Differentiation.** Saad thinks "metadata only" plus official status pages is not enough to
  charge for. Ideas discussed: bring back the cross-user "is it everyone?" signal (dropped in D6)
  once there are a few hundred users; alerts that say whether a spike is likely the provider's
  side; monthly provider-reliability reports usable for SLA claims; privacy as the reason
  regulated companies can use it.
- **Business model.** Competitors (Langfuse $29+/month, LangSmith $39/seat, Helicone $79) use a
  free tier then charge for alerts, retention, teams and volume. Our infrastructure is ~$25/month
  shared once Neon is on the paid plan, so one or two paying users cover it. Paid features stay
  parked until a waitlist shows demand (brief §8).

## How Saad likes to work

- Cost-sensitive: nothing paid unless clearly necessary. Say what something costs before
  suggesting it.
- Explain platform and infrastructure terms plainly; he is learning them and wants to be able to
  explain every part in interviews. Keep answers concise; he asks for detail.
- He merges PRs himself on GitHub and sometimes pushes from VS Code, so fetch `main` before work.
- He wants this handoff file kept current at the end of each session.
