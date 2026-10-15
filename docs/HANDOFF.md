# Session handoff

Read this at the start of every session, after `CLAUDE.md`. Update it at the end of every
session, in the same PR as the session's other changes, so it reaches `main`.

**Last updated:** 2026-10-15 (M2 step 3)

## Where the project is

- **Done: M0 and M1.** The `gaslit` Python SDK (`sdk/python/`) has `init`, `track`, `flush`,
  `shutdown`, per-minute summaries, sampling, the bounded queue, the background sender, fork and
  exit handling, and debug mode. Privacy, fail-open tests and the overhead benchmark run in CI.
- **Server** (`server/`): FastAPI with `/healthz` and `/readyz` only. No tables, no ingest yet.
- **Deployment:** written but switched off (repo variables `PRODUCTION_ENABLED`, `INFRA_ENABLED`
  unset). Production only, no staging (ADR 0006). Nothing is hosted or paid for until M3 (ADR 0007).
- **In progress: M2**, the SDK integrations (below). Steps 1-3 are done.

## Decisions made recently (details in the ADRs)

| ADR | Decision |
|---|---|
| 0005 | Product name **Gaslit**, `gaslit.dev`. Deploy by CI after tests pass; Terraform manages Cloudflare |
| 0006 | Production only, no staging. Each deploy needs Saad's approval in GitHub |
| 0007 | Database on **Neon** (Free while testing, Frankfurt). One 256 MB Fly machine that scales to zero. No hosting before M3 |
| 0008 | **One shared database** for all users, with per-account limits via `entitlements()` (no per-user databases). **No rollup worker at launch**: the dashboard sums summaries at read time. Includes the multi-tenant checklist for M3 |
| 0009 | Host detection rules the brief left open: only the hostname is read, unknown Bedrock vendor is `custom`, Vertex global region is `global`, "local" means not globally routable |
| 0010 | OpenAI integration: wrap `create` on the resource classes, measure streams by swapping their inner iterator, dropped streams are `cancelled`, `openai` 1.30+ supported. Azure deployment names are hidden (**Saad to confirm**) |

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

## Next: M2 plan (small PRs, in order)

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
   (3.31.0); CI has an `sdk-compat` job. Overhead (benchmark): ~10-15 µs per call, under 1 µs per
   streamed item. Streams dropped unfinished are recorded via a lock-free queue in `_runtime`
   (`abandon()`), because a garbage-collection finalizer may run while an SDK lock is held. **Waiting for Saad:** on Azure the `model` argument is the customer's deployment
   name, so `model_requested` is reported as `azure-deployment` (ADR 0010 point 5). Not covered
   yet: `with_raw_response`/`with_streaming_response` (status and duration only), `parse()` and
   `stream()` helpers that bypass `create`, embeddings.
4. **Anthropic integration**: `messages.create` and `messages.stream`, sync and async.
5. **Examples** (`examples/`) that print what the SDK sends in debug mode.

Before 4: check the current `anthropic` Python SDK internals against its docs (brief §17). Step 3
found that the SDKs have moved on: `openai` 3.x uses `httpx2`, so read the installed source, not
memory. Step 4 should reuse `_Measure`/`_runtime.abandon` from `_openai.py` where the shapes match
(move them to a shared module then, not before). Never capture prompts, outputs, keys or error message text (P1-P7).

## Loose ends

- `sdk/python/src/gaslit/_config.py`: `DEFAULT_ENDPOINT` is still `https://ingest.gaslit.invalid`
  with a stale "until the name is chosen" comment. Change it to `https://ingest.gaslit.dev`
  before the first real release (0.1.0, M7).
- Neon connection strings include `channel_binding=require`; asyncpg forwards it to Postgres,
  which rejects it. Remove it from `DATABASE_URL` (runbook step 2).
- `make test` needs Docker for the server's database test. Cloud sessions have no Docker: run
  `uv run pytest` in `sdk/python`, and in `server` with `-k "not with_database"`.

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
