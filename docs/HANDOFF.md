# Session handoff

Read this at the start of every session, after `CLAUDE.md`. Update it at the end of every
session, in the same PR as the session's other changes, so it reaches `main`.

**Last updated:** 2026-10-21 (M3 step 8: edge secret header written, ADR 0027; Saad's Cloudflare steps 3, 5, 6 next)

## Where the project is

- **Done: M0 and M1.** The `gaslit` Python SDK (`sdk/python/`) has `init`, `track`, `flush`,
  `shutdown`, per-minute summaries, sampling, the bounded queue, the background sender, fork and
  exit handling, and debug mode. Privacy, fail-open tests and the overhead benchmark run in CI.
- **Done: M2.** OpenAI and Anthropic integrations, host detection, fake provider, examples
  (PRs #13-18). Notes kept below under "M2 notes".
- **Server** (`server/`): FastAPI with `/healthz` and `/readyz`. Schema and Alembic (M3 step 1).
  Ingest keys (step 2), `POST /v1/ingest` (step 3, `app/ingest/`), rate limit and quota (step 4,
  `app/ingest/limits.py`, `app/core/entitlements.py`). The examples send to the
  local server: `make dev`, `make dev-key`, then `GASLIT_ENDPOINT=http://127.0.0.1:8000` and the
  printed `GASLIT_KEY` (`examples/README.md`).
- **Deployment:** written but switched off (repo variables `PRODUCTION_ENABLED`, `INFRA_ENABLED`
  unset). Production only, no staging (ADR 0006). Nothing is hosted or paid for until M3 (ADR 0007).
- **In progress: M3** (ingest and storage), plan below. Steps 1-7 are merged (#32-#40).
  Hosting (step 8) is next; confirm costs with Saad first. Saad chose to start on
  `shared-cpu-1x` and upgrade only once there are many users.

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
| 0013 | First schema: day partitions made by `app.db.partitions`, `NOLOGIN` roles `ingest_writer`/`dashboard_app` with RLS on data tables, ingest batches with `INSERT ... unnest` because `COPY` is refused under RLS, one column per error class, Alembic over asyncpg |
| 0014 | Dependabot opens PRs for **security fixes only** (alerts and security updates enabled 2026-10-17). Routine upgrades by hand at each milestone start: `uv lock --upgrade` in both packages, action SHAs, base image tags |
| 0015 | Ingest keys: `gl_live_` + 43 base62 + 6-character CRC32 (over the prefix too), 57 characters. Only keys found are cached (60 s, 10,000 max); the lookup is `UPDATE ... last_used_at ... RETURNING`, so it also records use, once a minute per key. Database errors propagate, never cached |
| 0016 | Ingest endpoint: cheap checks first (size, type, key, body caps), records validated one by one (`202` with `{array, index, reason}`, reasons never echo input), host allowlist re-checked, summary counts must add up, 415 for wrong type/encoding, 413 over 1,000 records, any DB error → 503. Two `unnest` inserts, no transaction |
| 0017 | Rate limit and quota, sized so free accounts fit the Neon bill: `entitlements(plan)` (free: 1M calls and 2M records a month, 20 records/s with bursts to 3,000, per account; placeholders). Neon compute fixed at 0.25 CU (~$19.35/month cap); storage per free account ~$0.005 typical, ~$0.10 if abused. Plan comes from the cached key lookup. Both checked from memory before the body is read. Usage counted in memory, added to `monthly_usage` every 10 s (one statement, returns totals from all machines). Rate limit is a per-machine token bucket. Keys of deleted projects refused |
| 0018 | SDK error samples: the first 3 of each error class per minute per process (was up to 100 a minute); the rest only counted. A capped error doesn't use up its series' `first_of_series` sample. No wire change |
| 0019 | Accounts over quota are answered from memory: an expired cached key is kept while its account is blocked, so SDKs' checks every minute never reach the database. The key lookup also returns this month's usage, so a new machine blocks at once. `Quota.unblock(user_id)` is for the upgrade flow, which must call it on every ingest machine |
| 0020 | Partition upkeep and retention run inside the app: at startup, then once per UTC day (hourly date check), under an advisory lock, `lock_timeout` 1 s. Days before today − `longest_retention_days()` are dropped, so rows live 7-8 days. Needs the table owner: how, once the app stops being the owner, is a step 8 decision |
| 0021 | Prometheus metrics with `prometheus_client` on their own port (`METRICS_PORT`, 9091), scraped by Fly into its free Prometheus/Grafana; never on the public port; labels fixed codes only. Requests by status and reason, latency, records, batch size, insert time, key lookups that reach the DB, quota flushes, last partition run |
| 0022 | Dashboard reads: one `sum()` per histogram bucket (5× faster than `unnest`, 20× faster than a SQL aggregate), behind `MetricsRepository` (`overview`, `series`, `Filters`). Each read is a read-only transaction that sets `app.project_id` for RLS. `quantile()` interpolates inside buckets. ~5 µs per summary read. **Proposed, for Saad:** p95 under 100 ms per query. 7-day reads of busy accounts took ~650 ms: fixed by 0023 |
| 0023 | 5-minute and hourly totals (`totals_5m`, `totals_1h`), built 3 min after each 5-minute bucket ends by whichever machine is receiving summaries (checks every minute; never wakes an idle database) and by the daily upkeep. `totals_watermark`: totals before it, summaries after, so reads are always correct. Late summaries (≥ 2 min after their bucket) are marked by ingest in `totals_stale` and rebuilt next build. Reads split a range into ≤ 5 pieces (summaries, 5-min, hourly, 5-min, summaries). Per-minute summaries and 5-min totals kept 2 days, hourly and samples 7. Busy account: 5 / 7 / 10 ms for 1 h / 24 h / 7 d (was 6 / 93 / 651). Saad approved 2026-10-18 |
| 0024 | Load test with our own open-loop generator (`tools/loadtest/`, reuses `tests/records.py`; not k6/Locust), locally on Docker limits copying production: app image in 256 MB on 1 core ("burst") or 5 ms/80 ms ("baseline", Fly's `shared-cpu-1x` quota once its burst balance is spent); Postgres at 0.25 CPU (Neon 0.25 CU). Rerun on Fly and Neon at step 8 |
| 0025 | Load shedding: past 50 ingest requests in progress, `429 overloaded` with a random 3-10 s `Retry-After` (not 503: the SDK drops a batch after three 5xx). `MAX_DECOMPRESSED` 2 MB; a rejected body costs the account one rate-limit token per KB (corrupt gzip: the cap). No per-request limit per account (Saad) |
| 0026 | **Worker app** `gaslit-worker-production` (no public port, always running, ~$2/month) alone holds Neon's owner URL: migrations as its `release_command`, `maintain` (partitions, totals, retention) at start and hourly. Ingest logs in as `gaslit_ingest` (member of `ingest_writer`), made by `python -m app.db.login_role ingest` over `fly ssh console`, piped into `fly secrets import`. Ingest no longer builds totals. Deploy: worker first, own token each |
| 0027 | `*.fly.dev` bypass closed by a secret header: Cloudflare sets `X-Gaslit-Edge` (Terraform, sensitive var `edge_secret` from GitHub secret `EDGE_SECRET`); the app (`app/core/edge.py`) answers 403 without it, except `/healthz`, and strips it. On when the Fly secret `EDGE_SECRET` is set; two space-separated values during a rotation. Infra workflow can now be run by hand |
| 0011 | Anthropic integration: same approach, shared code in `_wrap.py`; `messages.stream()` measured by wrapping the manager's enter; in-stream `error` events classified by type; input tokens include the prompt cache; Bedrock ARNs reduced to the public model id or `bedrock-arn` (confirmed); `anthropic` 0.28+ supported |

## Platform status (outside the repo)

| Item | Status |
|---|---|
| Domain `gaslit.dev` | Bought on Cloudflare Registrar 2026-10-14. DNSSEC was "pending" (Cloudflare adds the DS record itself) |
| Email-spoofing records (null MX, SPF `-all`, DMARC reject) | Done |
| PyPI name `gaslit` | Claimed: placeholder 0.0.0 uploaded 2026-10-14. The upload token was deleted |
| GitHub branch protection on `main` | Done |
| GitHub secret scanning | Saad chose not to enable it |
| Dependabot alerts and security updates | Enabled 2026-10-17 (ADR 0014). Version-update PRs are off |
| Repo visibility | **Public**, which keeps environment approval gates free (ADR 0006). D5 said private; Saad has not changed it |
| Neon, Fly apps | **Live 2026-10-21** (runbook step 2): Neon Frankfurt, `gaslit-worker-production` and `gaslit-api-production`; `/readyz` ok as `gaslit_ingest` |
| GitHub `production` environment, `PRODUCTION_ENABLED` | **Done 2026-10-21** (step 4): reviewer Saad, both Fly tokens, `PUBLIC_URL`. Deploy run #37 (attempt 3) green. `infra` environment: not checked |
| Cloudflare R2 and tokens, Terraform | **Done 2026-10-21.** Account API token, R2 bucket, secrets and variables, `infra` environment (reviewer Saad), `INFRA_ENABLED`. First apply (#45): DNS for `app`/`ingest`, TLS settings, firewall, rate limit, edge header |
| Certificates, `EDGE_SECRET` on Fly | **In progress.** Runbook step 6. Behind the proxy Fly needs `_fly-ownership` TXT and an `_acme-challenge` CNAME (DNS-01) per host, read from flyctl's source since fly.io is blocked in cloud sessions; Terraform now makes both (`acme_challenge` in `hosts`). Then 6.4 sets `EDGE_SECRET` |
| Google and GitHub OAuth apps | M4 (runbook step 7) |

## Next: M3 plan (small PRs, in order)

1. **Schema: DONE** (`server/migrations/`, `server/app/db/partitions.py`, ADR 0013).
   `make migrate` = `alembic upgrade head` + this week's partitions. Tests (`-m db`) run in a
   fresh `gaslit_test` database next to `DATABASE_URL`'s and cover partitions, idempotent
   inserts, role grants and RLS.
2. **Ingest keys: DONE** (`server/app/ingest/keys.py`, ADR 0015). `generate()` returns the
   key once plus `key_hash`, `display_prefix`, `last4`; `KeyStore(pool).resolve(key)` returns
   `IngestKey(id, project_id)` or `None` (malformed, unknown or revoked). DB tests run it as
   `ingest_writer`. `DB_TIMEOUT_S` moved to `app/db`.
3. **`POST /v1/ingest`: DONE** (`server/app/ingest/{routes,schema,sink}.py`, ADR 0016).
   `app.state.keys` (`KeyStore`) and `app.state.sink` (`PostgresSink`) are made in the lifespan;
   tests swap in fakes after startup (`tests/test_ingest.py`). `tests/records.py` builds
   SDK-shaped records. `make dev-key` (`app/db/dev_key.py`). All four examples were sent to the
   dev server on 2026-10-17: every record accepted. Typical batch ~0.5 ms CPU, 1,000 records
   19-35 ms.
4. **Rate limit and quota: DONE, PR #32** (`server/app/ingest/limits.py`,
   `server/app/core/entitlements.py`, migration `0002`, ADR 0017). `429 quota_exceeded` with
   `Retry-After` of at most 60 s, `429 rate_limited` until the account's bucket refills.
   `IngestKey` has `user_id` and `plan`. `app.state.rate` and `app.state.quota` are made in the
   lifespan with a 10 s flush task and a final flush on shutdown; tests swap in fresh ones with
   fake clocks and patch `app.ingest.routes.entitlements` for small limits. Measured row sizes:
   639 B per summary, 356 B per sample, with indexes. **Still open:** Saad's monthly database
   budget, and a re-check of the numbers after the step 7 load test. The per-IP limit on
   invalid keys moved to step 8 (needs a trusted client IP).
   **SDK error-sample cap: DONE, PR #33** (ADR 0018): the first 3 errors of each class per
   minute per process; the rest are only counted.
   **Blocked accounts from memory: DONE, PR #34** (ADR 0019; Saad
   approved the fix 2026-10-18). `KeyStore(pool, keep=quota.blocks, learn=quota.learn)`: an
   expired key whose account is over quota is still served, so its 429s need no database. The
   key lookup left-joins `monthly_usage` for this UTC month, so a new machine blocks at the
   first request. `Quota.learn` keeps the larger total per month. `Quota.unblock(user_id)` has
   no caller yet: **the upgrade flow (M4/paid tier) must call it on every ingest machine**, or
   an upgraded account stays blocked until the month ends or a restart. **Open for step 8:**
   Fly's docs don't say whether one request a minute keeps a lone machine running. If it
   doesn't, restarts wake Neon again. Options: `min_machines_running = 1` (~$2/month) or
   `auto_stop_machines = "suspend"` (keeps memory). Measure once hosted; ask Saad.
5. **Retention: DONE, PR #35** (
   `server/app/db/partitions.py`, ADR 0020). `maintain(conn, today, keep_days)` makes
   yesterday to +7 days and drops days before today − 7; `maintain_daily` runs in the lifespan
   (startup, then at most once per UTC day). `python -m app.db.partitions` and `make migrate`
   run the same thing. Tests: `test_retention.py` (schedule), `test_retention_db.py` (drops,
   the lock, startup). **For step 8:** the job needs owner rights (checked: only the owner can
   create a partition); pick a second owner URL just for the job or a no-argument `SECURITY
   DEFINER` function, together with where migrations run. Expired days wait for the next start
   if the app is stopped by Fly for lack of traffic.
   **Prometheus metrics: DONE, PR #36** (`server/app/core/metrics.py`, ADR
   0021). New dependency `prometheus-client` 0.26.0. Started in the lifespan when
   `Settings.metrics_port` is set (`METRICS_PORT`; set in `production.toml` `[env]` and
   compose, which maps `127.0.0.1:9091`). `[metrics]` in `production.toml`. Counting happens
   in `routes._done` (every answer), after a successful insert (records, batch size), in
   `KeyStore.resolve` (DB lookups), `Quota.flush` and `maintain_daily`. Tests read
   `prometheus_client.REGISTRY` deltas; only one test starts the metrics server (stopping it
   takes 0.5 s). ~7 µs per accepted request. **At hosting:** set the Grafana alerts listed in
   `deploy.md` step 2; check whether Fly's scrapes keep a machine running. Dashboard query
   latency comes with step 6.
6. **Read-time sums: DONE, in a PR** (`server/app/dashboard/repository.py`, ADR 0022).
   `MetricsRepository(pool)`: `overview(project, start, end, filters)` per provider, route
   and model; `series(..., step, filters)` per step and input bucket. Returns `Sums` named as
   in the summary record. `quantile(hist, bounds, q)` (None under 20 values). Histogram bounds
   now in `app.ingest.schema`. Metric `gaslit_dashboard_query_seconds{query}`. Not wired into
   the app yet: M4's routes create it in the lifespan.
   **End-to-end test** (`tests/test_end_to_end.py`): starts the app under uvicorn on a free
   port, runs every example with `uv run --locked` in `sdk/python` and
   `GASLIT_ENDPOINT`/`GASLIT_KEY` set, then checks every printed record is stored and the
   repository's sums (as `dashboard_app`) equal the payloads' sums by model and by minute.
   Also compares the server's provider/route enums, host allowlist and bounds with the SDK's
   tables. Takes ~8 s. CI's server job now syncs the SDK environment too.
   **Measured** (ADR 0022, local, warm, one core): ~5 µs per summary in range. Typical account
   (20k summaries a week): 2 / 14 / 107 ms for 1 h / 24 h / 7 d. Busy (121k): 6 / 93 / 651.
   **Open, for Saad:** (a) the target, proposed p95 under 100 ms per query; (b) 7-day reads
   of busy accounts miss it. Proposed fix: hourly totals for hours older than 24 h, which
   ingest can no longer change, built by the daily partition job (no extra cost) or hourly
   (~$1.60/month more on Neon). Measured 68 ms instead of 620 ms. `rollups_1m` would not help
   7-day charts. The benchmark scripts were throwaway (session scratchpad); step 7's load
   test should time the repository on Neon at 0.25 CU.
   **Then: 5-minute and hourly totals, in PR #38** (`server/app/db/totals.py`, migration
   `0003`, ADR 0023; Saad asked for every range to be fast, detail past a day can be coarser).
   `Totals` in the lifespan (`app.state.totals`); ingest calls `totals.note(summaries)` after
   storing; the sink writes late marks right after the summaries. `maintain(conn, now,
   keep_days)` now takes a datetime, builds before dropping, and catches up partitions after
   long pauses; `retention()` gives each table's first kept day, and never drops summaries
   not yet in totals. Tests: `test_totals.py` (when a machine builds), `test_totals_db.py`
   (totals equal the summaries, late rebuilds, lock, catch-up, RLS), and every repository and
   end-to-end read checked from summaries and from totals. Tests that need totals set
   `totals_watermark` themselves, since it is shared by the test database.
   **Measured** (ADR 0023): every range 5-30 ms for the busy account; one build 16 ms for all
   accounts; test-data storage 624 → 308 MB.
7. **Load test: DONE locally, in a PR** (`tools/loadtest/`, `docs/capacity.md`, ADR 0024).
   `tools/loadtest/run.sh up` starts Postgres (0.25 CPU, :5433, `pg_stat_statements`), makes
   1,000 accounts (keys in the git-ignored `tools/loadtest/keys.txt`) and starts the app
   (:8001, metrics :9092); `make loadtest ARGS="run ..."` drives it. `tests/test_loadtest.py`
   checks the generator's bodies are accepted in full.
   **Results:** 150 req/s (335 records/s) at p95 under 200 ms, limited by database CPU; 3,000
   records/s in 200-record batches. App ~2.2 ms CPU per request, so a `shared-cpu-1x` machine
   keeps up ~25 req/s on average and ~10 req/s at good latency once its burst balance is spent.
   Spike and database outage handled well (503 after 2 s, recovery in 5-10 s). Past saturation
   ingest collapses rather than plateaus. A 2,000/s bad-key flood pegs the app (memory 166 MB);
   zip bombs cost 11 ms each. Dashboard 24 h/7 d charts take 70-200 ms on 0.25 CU.
   **Then, PR #40 (ADR 0025, Saad's choices on the proposals):** load shedding (past 50
   requests in progress, `429 overloaded`, random 3-10 s `Retry-After`; 429 because the SDK
   keeps a batch after a 429 but drops it after three 5xx), `MAX_DECOMPRESSED` 2 MB, and
   rejected bodies charged one rate-limit token per KB. Overload now stores 290-340 records/s
   at 300-400 req/s instead of ~10; a junk flood stops after ~40 bodies. **Dropped (Saad):** a
   per-request limit per account; usage within the quota must never be throttled by how it is
   spread over processes. This was the step 4 re-check of ADR 0017: storage per row holds.
   **Still open:** the dashboard target on 0.25 CU (proposal 5, for M4), grouping inserts
   (later), quieting `ClientDisconnect` tracebacks (one-line fix).
8. **Hosting** (runbook steps 2-6), with login roles created by SQL and granted
   `ingest_writer`/`dashboard_app`. **Costs approved by Saad 2026-10-21.** Saad's answers so
   far: scale to zero stays as is until he says otherwise (explained that a sending account keeps
   the machine running anyway, so it only matters when nobody sends), and close the
   `*.fly.dev` bypass with the secret Cloudflare header. **Worker app and ingest login: done,
   in a PR** (ADR 0026; Saad chose "ingest only writes, cleanup is its own job"). `app/worker.py`, `maintain_every` in
   `app/db/partitions.py` (replaces `maintain_daily`), `app/db/login_role.py`,
   `deploy/fly/worker.toml`, worker step in `deploy.yml` (secret `FLY_WORKER_API_TOKEN`),
   compose `worker` service. `Totals` (in-app builder) and its two metrics removed. The
   end-to-end test now runs the app as `gaslit_ingest`; checked with passwords required.
   Runbook step 2 rewritten for the two apps. **Hosting progress (2026-10-21):** worker
   deployed, migrations ran on Neon, first upkeep logged; the `fly ssh console ... | fly
   secrets import` pipe worked (flyctl's "Connecting to" line goes to stderr). flyctl reads
   `[build] dockerfile` relative to the config file, so both configs dropped it (fix PR on
   this branch). Deploy workflow green on 2026-10-21 (worker, then app, then `/readyz`).
   Gotcha: the environment must be named exactly `production`; a job naming a missing
   environment makes GitHub create an empty one (no secrets, no reviewer). Then rerun the load test
   against Fly and Neon (`docs/capacity.md`, "How to rerun") and put the number in the README.
   Floods (from step 7): rerun the bad-key flood through Cloudflare, add the app's
   per-IP limit on invalid keys, and check whether a machine that scales to zero restarts with
   only a 5 s burst balance.
   **`*.fly.dev` bypass: done in code, branch `c/blissful-wright-3tvuao`** (ADR 0027): Terraform rule
   `cloudflare_ruleset.edge_secret`, `app/core/edge.py`, `Settings.edge_secrets`
   (`EDGE_SECRET`), `tests/test_edge.py`, runbook steps 3.5, 4, 5, 6.4 and the rotation. The
   Terraform was validated locally with provider 5.28.1 (registry blocked in cloud sessions:
   binary from GitHub releases into a filesystem mirror); a plan can't run offline because the
   ruleset resource does a dry run against Cloudflare's API, so check the first CI plan shows
   the header value as sensitive. **Next PRs:** (a) per-IP limit on invalid keys, reading
   `CF-Connecting-IP` only behind the edge check; (b) once `EDGE_SECRET` is set on Fly, make
   `load_settings()` require it on Fly; (c) TLS and the edge header in the load generator for
   the Fly rerun (it speaks plain HTTP only).

## Dashboard direction (Saad, 2026-10-17; for M4, and steps 6-7 now)

The dashboard must be **clear, well organised and visually appealing**, and **fast to load and
to change** (switching range, filters or pages should feel instant). Both count equally.

- **Clarity first.** Brief §10.2 says "dense, dark-first"; density must never cost clarity.
  Clear hierarchy (health summary first, details on demand), consistent spacing and units,
  plain labels, and §10.3's display rules. Ask Saad whether "dense" still stands.
- **Agree the look before coding.** Start M4 with a mockup of the overview and model detail
  pages for Saad to approve, then build.
- **Speed is designed in:** brief §21's budget (~200 KB gzipped JS, uPlot only, lazy-loaded
  detail pages), no layout shift while loading, cached responses so changing a filter only
  fetches what changed.
- **It starts in M3.** Step 6's reads decide how fast a range or filter change is. Totals
  (ADR 0023) make every range ~5-30 ms on a whole core, but 70-200 ms for 24 h/7 d charts on a
  0.25 CPU database (step 7, `docs/capacity.md` proposal 5). Target proposed in ADR 0022 (p95
  under 100 ms per query, awaiting Saad); measure again on Neon at step 8.
- Keep the frontend simple to change: few small components, no UI framework sprawl.
- **Show recovery quickly (Saad).** When a provider comes back, the dashboard must say so within
  minutes, not after the 15-minute badge window: a "recovering" badge once the last 2 complete
  minutes are back within the thresholds (brief §7.3, starting point). The live feed shows
  successful calls again within seconds; error samples are the first 3 of each type per minute
  (ADR 0018), so the feed must say error totals come from the summaries.
- **Upgrades resume monitoring (Saad).** A free account over quota that upgrades must see data
  again at once. Ingest side (ADR 0017, ADR 0019): the upgrade sets `users.plan` **and calls
  `Quota.unblock(user_id)` on every ingest machine**; SDKs check back every 60 s, so sending
  resumes within a minute, and queued records under 24 h old fill part of the gap. Dashboard
  side (M4, and the paid tier when it exists): the over-quota banner must clear from
  `monthly_usage` against the new plan's limits, and the usage display should show the gap
  honestly if one remains.

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
4. **Anthropic integration: DONE** (`sdk/python/src/gaslit/_anthropic.py`, ADR 0011).
   `messages.create` (sync/async, streaming or not) and the `messages.stream()` helper; the
   Bedrock, Vertex and Foundry clients share the same classes. The shared wrapper and stream code
   moved from `_openai.py` to `_wrap.py`. Tested on anthropic 0.28.0, 0.50.0, 0.124.0, 1.0.0 and
   1.15.0 (latest); the `sdk-compat` CI job now runs both libraries (older ones need
   `httpx<0.28`). Overhead: ~13 µs per call, ~0.4 µs per streamed event. The fake provider
   gained `cached_tokens` and `stream_error` (an Anthropic `error` event after HTTP 200).
   Saad confirmed the Azure and Bedrock ARN rules. Not covered, by choice for now: `beta.messages`
   (and the tool runner), `messages.parse()`. Saad is fine with the newer Bedrock Mantle,
   `AnthropicAWS` and `AnthropicGoogleCloud` hosts staying `custom`.
5. **Examples: DONE** (`examples/`, ADR 0012). `openai_chat.py`, `anthropic_summariser.py`,
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

- **Stacked PRs:** before deleting a merged PR's branch, retarget the next PR to `main`
  (`gh pr edit N --base main`). Deleting a base branch through the API closes the PRs on it;
  GitHub retargets them only when it deletes the branch itself. Happened with #35 on
  2026-10-18 (restored the branch, reopened, retargeted).

- **Security config:** `docs/security-config.md` lists every setting that keeps users'
  data safe, with status. Check it at each step. The `*.fly.dev` bypass stays open until
  runbook step 6.4 sets `EDGE_SECRET` on Fly (ADR 0027).

- `sdk/python/src/gaslit/_config.py`: `DEFAULT_ENDPOINT` is still `https://ingest.gaslit.invalid`
  with a stale "until the name is chosen" comment. Change it to `https://ingest.gaslit.dev`
  before the first real release (0.1.0, M7).
- Neon connection strings include `channel_binding=require`; asyncpg forwards it to Postgres,
  which rejects it. Remove it from `DATABASE_URL` (runbook step 2).
- The server's tests include `test_end_to_end.py`, which runs the examples through `uv run`
  in `sdk/python`, so they need `uv` and the SDK's locked environment (synced on first run).
- `make test` needs Docker for the server's database tests. Cloud sessions have no Docker: run
  `uv run pytest` in `sdk/python`, and in `server` with `-m "not db"`. On 2026-10-18 `docker`
  was not on the WSL path (Docker Desktop not running or its WSL integration off), so the DB
  tests ran against a portable Postgres 17.10 in the session scratchpad, with `DATABASE_URL`
  set (`make db` then skips compose).
- Docker works in WSL again (2026-10-18, step 7). The load-test stack runs beside compose
  (Postgres :5433, app :8001, metrics :9092, network `gaslit-load`); `tools/loadtest/run.sh down`
  removes it. It never touches the dev database.
- Partitions, totals and retention run in the worker (compose service `worker` under
  `make dev`), hourly, so the dev database no longer runs out a week after `make migrate`.
- Starlette's `TestClient` uses `httpx2` (the dev dependency), not `httpx`.
- Soft-deleting a project must revoke its keys in the same transaction (M4, ADR 0015).
- Hosted ingest logs in as `gaslit_ingest` (ADR 0026); locally (compose, tests' app) it is
  still the owner. The dashboard's login (`login_role dashboard`) comes with M4.

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
