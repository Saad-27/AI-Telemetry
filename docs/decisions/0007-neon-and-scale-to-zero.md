# 0007: Neon free database, scale-to-zero Fly machine, nothing paid before M3

- **Status:** accepted
- **Date:** 2026-10-14

## Context
Saad does not want to pay for anything until it is needed. ADRs 0005 and 0006 assumed Fly
Managed Postgres, which starts at about $38/month with no free tier, and one production machine
kept warm. Until M3 the server only answers `/healthz` and `/readyz`, so there is nothing worth
paying to host. Fly has no free allowance for new accounts, so even an idle machine costs a little.

## Options considered
1. **Fly Managed Postgres** (brief §9.8 default). Private network, automated backups and
   point-in-time recovery. About $38/month from day one.
2. **Neon Free.** Managed serverless Postgres with a Frankfurt region. Free within 0.5 GB of
   storage and 100 compute-hours a month per project. The compute sleeps when idle and wakes on
   the next connection. Short point-in-time restore window. Reachable over the internet.
3. **Supabase Free.** Similar limits, but it bundles auth, storage and an API we would not use,
   and pauses inactive projects.

For the app machine: keep one warm (a few dollars a month), or scale to zero (billed only while
running, with a cold start of a few seconds after idle).

## Decision
- **Database:** Neon Free, region AWS Europe Central 1 (Frankfurt), so data stays in the EU (D12).
  The app uses the direct (non-pooled) connection string as the Fly secret `DATABASE_URL`, with
  `channel_binding` removed (asyncpg 0.31 sends unknown query parameters to Postgres as settings,
  and Postgres rejects that one). It is still plain Postgres, so D3 is unchanged and moving
  later is a `pg_dump` and a new secret.
- **App:** one `shared-cpu-1x` 256 MB machine that scales to zero (`min_machines_running = 0`,
  `--ha=false`).
- **Timing:** no hosting until M3. Platform setup steps 2-6 in `docs/deploy.md` move to M3. The
  domain and the PyPI name were claimed now because they could be taken.

## Trade-offs
- **Cold starts.** After idle, the first request waits for both the Fly machine and Neon compute
  to wake. The SDK sends in the background and retries, so users' apps never wait, but the first
  dashboard load after a quiet spell is slow. Raise `min_machines_running` to 1 when real users
  would notice.
- **Storage cap.** 0.5 GB fills with indexes and WAL sooner than the raw numbers suggest. The M3
  load test and `docs/capacity.md` should estimate when, and the first upgrade is Neon's paid tier
  or Fly Managed Postgres.
- **Backups and network.** Neon Free's restore window is short and the database is reachable from
  the internet (password and TLS only). Both must be revisited before the private alpha, against
  the brief §11.11 checklist (tested restore, least-exposed database).
- **Compute-hours.** Once real users send data continuously, ingest alone keeps the compute
  awake around the clock, which uses more than the free 100 compute-hours a month (about day 16).
  Move to Neon's paid plan then (~$19/month at 0.25 CU). The every-minute rollup worker is
  dropped so it does not add load in the meantime ([ADR 0008](0008-shared-db-read-time-aggregation.md)).
