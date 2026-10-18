# Capacity

Load-test results for `POST /v1/ingest` (brief §20.6). The method is in
[ADR 0024](decisions/0024-load-test-method.md). Rerun the test at hosting (M3 step 8) and before
each stage change (brief §20.3).

The results are from before the fixes in
[ADR 0025](decisions/0025-load-shedding-and-rejected-body-charges.md) (load shedding, charges for
rejected bodies), except under "After the changes" at the end.

## Headline (local run, 2026-10-18; not yet on Fly and Neon)

| Setup | Sustained, p95 under 200 ms | What limits it |
|---|---|---|
| App on 1 core (a `shared-cpu-1x` machine with burst balance), Postgres 0.25 CPU | **150 req/s, 335 records/s** with the SDK's usual mix (~2.2 records a request). **3,000 records/s** in full 200-record batches | Database CPU (pegged at 0.25) |
| App at 6.25% of a core (a `shared-cpu-1x` machine whose burst balance is spent) | **About 10 req/s.** At 30 req/s the CPU quota is used up and requests fail | App CPU |

- The brief estimates **~0.2 req/s per active SDK process** (§20.4).
  - At 150 req/s, the database handles about **750 processes**.
  - A `shared-cpu-1x` machine can keep up an average of about 25 req/s, so about **125
    processes**. That average is its 6.25% quota divided by 2.2 ms per request.
  - It can serve up to 150 req/s while its burst balance lasts: about 30 minutes from a full
    500 s balance.
- **Each request costs about 2.2 ms of app CPU** (container cgroup; 1.8 ms when profiled on the
  host). On the database it costs about 1.5 ms of CPU, of which 0.5-0.9 ms is the insert
  itself.
- **Per-request overhead, not per-record cost, limits the database.** With 200 records a
  request, it stores about 10× more records per second.

## Setup

| Part | As run | Stands in for |
|---|---|---|
| App | Production image (`server/Dockerfile`), 1 uvicorn process, 256 MB. `--cpus=1` (burst) or `--cpu-period=80000 --cpu-quota=5000` (baseline) | Fly `shared-cpu-1x`, 256 MB (`deploy/fly/production.toml`) |
| Database | `postgres:17-alpine`, `--cpus=0.25`, 1 GB, local disk, `pg_stat_statements` on | Neon at 0.25 CU (ADR 0017) |
| Data | 1,000 free accounts, one key each. A week of history for a busy account (121k summaries) and a typical one (20k) | |
| Generator | `tools/loadtest/loadtest.py`: 4 processes, up to 64 keep-alive connections each, 5 s timeout | SDK processes behind Fly's proxy |
| Host | AMD Ryzen 7 2700X (16 threads), WSL2, Docker Desktop | |

The usual mix copies the SDK: 11 requests in 12 carry 1-3 samples (sent every 5 s), and 1 in
12 carries a process's 4 summaries for the minute plus a sample.

## Results

Latency counts from each request's scheduled send time (open loop), so it includes any time
spent waiting for a connection. Each step ran for 30-90 s after a warm-up, which absorbs the
first lookup of each of the 1,000 keys.

### 1. Steady load

**App on 1 core:**

| Rate | p50 / p95 / p99 (ms) | Errors | App CPU | DB CPU |
|---|---|---|---|---|
| 50 req/s | 10 / 22 / 98 | none | 0.14 | 0.08 |
| 100 req/s | 8 / 15 / 20 | none | 0.23 | 0.14 |
| 150 req/s | 9 / 21-36 / 50-100 (one run had a 10 s window at p95 920) | 0.1% 503 in one run | 0.33 | 0.19 |
| 200 req/s | 1,360 / 2,690 / 2,920 | 2-3% 503 | 0.40 | **0.25 (cap)** |
| 300 req/s | 5,000 (timeouts) | **almost all**; ~10 records/s stored | 0.5-0.7 | 0.25 |

**Collapse instead of a plateau.** Past saturation, requests queue for the 5-connection pool
(503 after 2 s). Clients time out at 5 s, but the app still works on their abandoned requests.
At 300 req/s it stored almost nothing. Proposal 2 below fixes this.

**App at 6.25% of a core (baseline):**

| Rate | p50 / p95 (ms) | Errors |
|---|---|---|
| 10 req/s | 12 / 190 (after warm-up) | none |
| 20 req/s | 12 / 450-620 | none |
| 30 req/s | 1,000 / 2,600 | 10% 503 |
| 40-60 req/s | 2,300-4,000 / 5,000 | most requests |

At 10 req/s, the average CPU (0.04) is under the quota, yet p95 still reaches 190 ms. Any burst
over 5 ms in an 80 ms period, such as a quota flush or a burst of key lookups, waits for the next
period. On Fly an unspent balance absorbs these, so this mode is the worst case.

**Full batches (200 records, as after a backlog):**

| Rate | Records/s | p50 / p95 (ms) |
|---|---|---|
| 5 req/s | 1,000 | 39 / 47 |
| 10 req/s | 2,000 | 50 / 95-300 |
| 15 req/s | 3,000 | 46 / 124 |
| 20 req/s | 3,640 | 1,360 / 4,040, 9% 503 |

**Database time per statement** (`pg_stat_statements`, usual mix):

| Statement | Mean | Share |
|---|---|---|
| `INSERT INTO call_samples` (1-3 rows) | 0.50 ms | 74% |
| `INSERT INTO summary_records` (4 rows) | 0.87 ms | 11% |
| `monthly_usage` flush (1,000 accounts, every 10 s) | 97-162 ms | 7-9% |
| Key lookup (`UPDATE ingest_keys`, once a minute per key) | 0.20 ms | 4% |
| asyncpg's reset when a connection returns to the pool | ~0.02 ms | 2% (one extra round trip a request) |

Execution time is only about half the database's CPU. The rest goes on each request's round
trips, commits, parsing and the pool reset.

### 2. A sudden 10× spike (15 → 150 → 15 req/s, app on 1 core)

No errors. p95 was 253 ms in the first 5 s of the spike, 187 ms in the next 5 s, then 37-110 ms.
It returned to normal at once afterwards.

### 3. Slow or unavailable database (50 req/s, app on 1 core)

- **Paused for 20 s** (`docker pause`): every request got a 503 (`Retry-After: 5`) after
  exactly 2.0 s, which is `DB_TIMEOUT_S`. The app stayed up, using 56 MB. It was back to normal
  5-10 s after the database returned. Three quota flushes failed and were retried; no counts
  were lost.
- **Slowed to 0.05 CPU for 30 s:** about 45% of requests got a 503, and p95 rose to 4.2 s,
  close to the SDK's 5 s timeout. It recovered within 5 s. A 202 can take up to about 8 s
  (2 s to get a connection, plus 2 s per statement). The SDK times out first and retries, and
  the ids make the retry safe.
- **Fly limit:** at 2 s per failed request, 150 req/s means about 300 requests in flight, which
  is over `hard_limit = 250`. Fly's proxy would then queue or refuse requests.

### 4. Invalid-key flood (with 50 req/s of real traffic, app on 1 core)

- **Unknown but well-formed keys, 200/s** (each one looked up in the database, since only keys
  found are cached): answered 401 at p95 5.5 ms. Real traffic was unaffected (p95 18 ms). The
  database used 0.17 CPU. No WAL is written, because no row matches.
- **Keys that fail the checksum, 2,000/s** (no database work): the app's core was pegged. It
  answered about 1,460/s for 15 s, then everything timed out, real traffic included. Memory
  peaked at **166 MB** of 256. One machine cannot absorb a flood like this. Limits at the edge
  are needed (Cloudflare, and the per-IP limit planned for step 8).

### 5. Oversized and malformed payloads (100/s with 50/s real, app on 1 core)

The junk was a mix of malformed JSON, a zip bomb (6 MB of zeros, 5.8 KB sent) and 1,001
records. Every one was rejected correctly (400 or 413). They cost **0.75 of a core**, and real
traffic's p95 rose to 140-850 ms.
- **The zip bomb costs 11 ms of CPU**, inflated up to the 5 MB limit: 5× a normal request. It
  needs a valid key, and the rate limiter charged it 1 token (since changed, see below). So one free account (20 tokens/s,
  bursts to 3,000) could keep a `shared-cpu-1x` machine's quota busy on its own (proposal 3).
- 1,001 records cost 3.4 ms (gunzip and JSON parsing) before the 413.

### Dashboard reads (busy account, 7 days of history, database at 0.25 CPU)

One page load (overview plus chart, for 1 h, 24 h and 7 d) every 5 s:

| Read | Idle p50 / p95 (ms) | At 100 req/s ingest p50 / p95 (ms) |
|---|---|---|
| overview 1 h | 5 / 161 | 5 / 174 |
| series 1 h (1 min steps) | 9 / 70 | 8 / 16 |
| overview 24 h | 6 / 80 | 6 / 10 |
| series 24 h (15 min steps) | 70 / 97 | 84 / 184 |
| overview 7 d | 13 / 114 | 95 / 298 |
| series 7 d (1 h steps) | 95 / 183 | 188 / 203 |

These are 4-10× ADR 0023's figures (5-30 ms), which were measured on a whole core. A query
needing 25 ms of CPU takes at least 100 ms of wall time under a 0.25 quota. **On 0.25 CU the
proposed target (p95 under 100 ms) is missed.** Medians stay under 100 ms except the 7 d chart
under load. But the 24 h and 7 d charts take 70-190 ms, and the first read of each round, even a
cheap one, can wait up to about 170 ms for the CPU quota (proposal 5).

### Other measures

- **Database I/O (WAL):** about 1 KB per record with the usual mix and 0.55 KB in full batches.
  That is 0.2 MB/s at 100 req/s and 1.1 MB/s at 3,000 records/s.
- **Totals builds (the "rollup lag" of brief §20.6):** under load a build took 5 ms to 6 s, all
  successful. Totals are built up to 4-5 minutes after their bucket ends (`BUILD_AFTER` 3 min,
  checks every 60 s). Reads stay exact meanwhile, because they sum summaries for the minutes not
  yet built (ADR 0023).
- **App memory:** 48-104 MB in normal runs, 166 MB under the bad-key flood. 256 MB is enough.
- **Measured and rejected:** uvloop and httptools in uvicorn saved only about 10% of CPU per
  request (1.6 vs 1.8 ms), not worth two dependencies. A py-spy profile shows the time spread
  across uvicorn, Starlette and FastAPI (about 40%) and asyncpg (about 15%). Logging is 3%,
  metrics 1%, our parsing and validation about 8%.

## Decisions and changes (Saad, 2026-10-18)

| # | Proposal | Outcome |
|---|---|---|
| 1 | Machine size | **Stay on `shared-cpu-1x`** ($2.53/month in `fra`) until there are many users. Watch CPU throttling in Fly's Grafana. When needed: `shared-cpu-2x` 512 MB ($5.06, ~55 req/s sustained) or `shared-cpu-4x` 1 GB ($10.13, ~110 req/s); prices from Fly's per-second rates, 2026-10-18. Still to measure at step 8: whether a machine that scales to zero starts with only the initial 5 s burst balance |
| 2 | Shed load early | **Done** ([ADR 0025](decisions/0025-load-shedding-and-rejected-body-charges.md)). Past 50 requests in progress: `429 overloaded`, random 3-10 s `Retry-After`. 429 rather than 503, because the SDK keeps a batch after a 429 and drops it after three 5xx |
| 3 | Rejected bodies cost their size | **Done** (ADR 0025). `MAX_DECOMPRESSED` 2 MB (a bomb now costs 4.8 ms, was 11). One rate-limit token per KB of a rejected body; corrupt gzip costs the cap |
| 3b | Edge limits for floods | **Plan for step 8.** Cloudflare's per-IP rule (300 per 10 s) is in Terraform but is skipped while `*.fly.dev` is reachable (`security-config.md` §3). Close that gap, rerun the bad-key flood through Cloudflare, and add the app's per-IP limit on invalid keys |
| 4 | Per-request limit per account | **Dropped.** Legitimate use should never be throttled by how it is spread over processes. A free account within its 1M calls a month sends about one request a second even across 50 processes, and the existing bucket already caps tiny requests at ~10 a second. Revisit only if Grafana shows an account abusing it |
| 5 | Dashboard target on 0.25 CU | Open, for M4 |
| 6 | Group inserts across requests | Later, once sustained traffic nears ~100 req/s |
| 7 | Quiet client disconnects | Open (one-line fix) |

### After the changes (same local stack)

**Overload no longer collapses** (app on 1 core):

| Rate | Stored | 202 / 429 `overloaded` | p50 / p95 (ms) | Before |
|---|---|---|---|---|
| 150 req/s | 332 records/s | 98% / 2% | 11 / 263 | same |
| 200 req/s | 363 records/s | 80% / 20% | 231 / 523 | 442 records/s, p95 2,690, 2% 503 |
| 300 req/s | 287 records/s | 42% / 58% | 5 / 609 | **~10 records/s**, nearly all timed out |
| 400 req/s | 319 records/s | 35% / 65% | 5 / 561 | not run |

No timeouts and no 503s; a shed request is answered in about 5 ms. Accepted requests wait up to
about 0.5 s (p95) while shedding: 50 in progress at the database's pace. The SDK retries shed
requests 3-10 s later with its batch kept. The warm-up also shed a few requests (55) while the
1,000 keys were each looked up for the first time, as after a restart.

**Junk from 10 accounts, 100/s, with 50 req/s of real traffic:** about 40 junk bodies were
answered 400 or 413; the next ~4,460 got `429 rate_limited` before their body was read. App CPU
0.33 (was 0.87); real traffic p95 24 ms (was 140-850 ms), every request stored.

## How to rerun

```sh
tools/loadtest/run.sh up            # Postgres 0.25 CPU on :5433, accounts, app (1 core) on :8001
export DATABASE_URL=postgresql://app:load@127.0.0.1:5433/app
C="--containers gaslit-load-app gaslit-load-pg"
K="--keys ../tools/loadtest/keys.txt"
make loadtest ARGS="run $K --rate 50,100,150,200 $C"                    # 1. steady
make loadtest ARGS="run $K --rate 15,150,15 --every-s 5 $C"             # 2. spike
docker pause gaslit-load-pg; docker unpause gaslit-load-pg              # 3. during a run
make loadtest ARGS="run $K --rate 50 --flood unknown-key:200 $C"        # 4. invalid keys
make loadtest ARGS="run $K --rate 50 --flood junk:100 $C"               # 5. junk
make loadtest ARGS="run $K --kind large --rate 5,10,15,20 $C"           # full batches
make loadtest ARGS="run $K --rate 100 --step-s 90 --dashboard $C"       # dashboard reads
tools/loadtest/run.sh app baseline  # the app at 6.25% of a core; `app burst` to go back
tools/loadtest/run.sh down
```

**At step 8 (Fly and Neon):** point `--url` at the deployed app with keys made on Neon. Leave out
`--containers`, and read CPU, throttling and memory in Fly's Grafana. Then record:
- requests/s at p95 under 200 ms;
- how long the burst balance lasts;
- whether a stopped machine starts with a full balance;
- dashboard read times on real Neon.

Put the number in the README (brief §9.10).
