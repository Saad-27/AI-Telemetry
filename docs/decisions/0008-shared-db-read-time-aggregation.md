# 0008: One shared database with per-account limits; sum summaries at read time

- **Status:** accepted
- **Date:** 2026-10-15

## Context
Two questions came up while planning hosting on Neon (ADR 0007).

1. **Who pays for database compute, and how are free users limited?** Saad asked whether each
   user, or each paying user, needs their own database, so that free users hitting a limit
   would not affect anyone else.
2. **Is the every-minute rollup worker needed?** Brief §9.5 and §19.4 have a worker that sums
   the summaries from all of a project's app processes into `rollups_1m` every 60 s. On Neon,
   a job that queries every minute keeps the compute awake and billed around the clock.

Facts checked on 2026-10-15 ([Neon pricing](https://neon.com/pricing)): Neon's paid Launch plan
has no monthly minimum and bills $0.106 per CU-hour plus $0.35 per GB-month. The smallest
compute (0.25 CU) awake all month is about $19.

## Options considered
**Tenancy**
1. **One database per user**, or per paying user. Strong isolation, but each always-awake
   database costs at least ~$19/month, so 100 users would be ~$1,900 against ~$25 shared.
   Every migration, pool and backup is multiplied by the number of users. Users pay for
   features, not for a database of their own.
2. **One shared database, per-account limits in the app.** Brief §20.1. Cost grows with data
   volume, not with the number of users.

**Combining summaries**
1. **Rollup worker** (brief §9.5): pre-computes `rollups_1m` every minute. Cheap reads at any
   scale, but it is one more component and keeps the database busy even when nobody looks.
2. **Sum at read time**: the dashboard API sums `summary_records` for the requested range and
   series when a page loads. Histograms use fixed buckets, so the sums are exact (brief §9.4).

## Decision
- **One shared, multi-tenant database** for all accounts. Neon Free while only Saad is testing.
  Neon's paid plan (no cap, ~$20-40/month) once real users send data continuously. That cost is
  shared by everyone; one or two paying users cover it.
- **Free accounts are limited per account**, through `entitlements(plan)`: 1M calls measured a
  month, 7-day retention, 1 project (D7). An account over quota gets `429` on ingest and an
  upgrade banner. Other accounts are unaffected. "Compute-hours" are our bill, never a user limit.
- **No rollup worker and no `rollups_1m` table at launch.** The dashboard API sums
  `summary_records` at read time, using the index on `(project_id, minute)`. With 7-day
  retention a heavy free account holds ~30k summaries (brief §7.2), which Postgres sums in
  milliseconds. Late summaries (SDK retries, up to ~15 min) simply appear on the next read.
- The worker keeps only: the retention job (daily partition drops) and, in M5, the status
  poller, which writes only when a status page changes.
- **Add `rollups_1m` back when measurements say so:** dashboard p95 over ~300 ms in the M3 load
  test or in production metrics. The brief's §9.5 design is the plan for that.

## Multi-tenant checklist
Concerns of a shared pipeline, and where each is handled. Items marked **(M3)** are not built yet.

| Concern | Handling |
|---|---|
| Cross-tenant reads (most important) | Every query scoped by `project_id`, Postgres RLS (D10), IDOR tests **(M3/M4)** |
| Duplicate batches from SDK retries | Stable `summary_id`/`event_id` (ADR 0004); primary keys plus `ON CONFLICT DO NOTHING` **(M3)** |
| Late data | Read-time sums include it automatically |
| One account flooding ingest | Cloudflare per-IP limit (Terraform), per-key rate limit and quota **(M3)** |
| Database or server outage | SDK retries then drops; users' apps never wait (P4). A stream buffer is brief §20.3 stage 2 |
| Key lookup on every request | In-memory key cache, max 60 s (brief §9.2) **(M3)** |
| Clients with wrong clocks | Reject summaries whose `minute` is more than ~1 h in the future or older than retention **(M3)** |
| Several server machines (later) | Advisory lock so each job runs once; quotas counted in the database, not in memory |

## Trade-offs
- Read cost grows with data per account. Fine at 7-day retention and today's volumes, and the
  brief's rollup design is ready when it is not.
- Without the worker there is no "rollup lag" metric (brief §9.10). Dashboard query latency
  replaces it as the signal to watch.
- A shared database means one bad migration or noisy query affects everyone. Mitigations:
  Alembic migrations reviewed in PRs, statement timeouts on dashboard queries, per-key limits.
- Brief sections that mention `rollups_1m` (§4.3, §9.4, §9.5, §9.10, §14 M3, §19.4, §20.3)
  describe the later design; they carry a note pointing here.
