# 0026: A worker app holds the owner's login; ingest logs in as a member of ingest_writer

- **Status:** accepted (Saad, 2026-10-21). Replaces "in the app" in ADR 0020 and "who builds"
  in ADR 0023.
- **Date:** 2026-10-21

## Context
Until now the app connected as the schema owner, which bypasses RLS and can read or drop
anything (ADR 0013). Partition upkeep, retention and the totals build ran inside the ingest
app and need the owner: creating or dropping a partition is the owner's alone, and a build
reads every tenant's summaries (ADR 0020, ADR 0023). Migrations need the owner too.

Saad's rule (2026-10-21): ingest only writes, the dashboard only reads, and cleanup runs as
its own job, connected to neither.

Checked: Fly secrets belong to an app, so every machine of an app sees all of them. A
`release_command` runs with the app's secrets. Scheduled machines (`fly machine run
--schedule hourly`) are not part of `fly.toml`, so `fly deploy` neither creates nor updates
them, and `fly machine run` returns when the machine starts, not when it exits.

## Options considered
1. **A second, owner URL in the ingest app.** Simplest, but the owner's password is on every
   ingest machine.
2. **`SECURITY DEFINER` functions** the ingest login may call. The owner's password stays off
   Fly, but upkeep and the totals build would be rewritten in SQL.
3. **A separate Fly app with a scheduled machine.** Billed only while it runs, but CI must
   create the machine, keep its image current with `fly machine update`, and run migrations
   with its own polling for an exit code.
4. **A separate Fly app with one always-running machine** (the brief's `worker`, §9.8),
   deployed with `fly deploy` like the ingest app. Migrations are its `release_command`.

## Decision
Option 4.
- **`gaslit-worker-production`** (`deploy/fly/worker.toml`): no public port, its
  `DATABASE_URL` is Neon's owner URL, and it is the only app that has it.
  `python -m app.worker` runs `app.db.partitions.maintain` at startup and then every hour:
  partitions through 7 days ahead, totals for every ready bucket, then expired days. Each run
  opens its own connection and closes it, so none is left open between runs.
- **Migrations** are the worker's `release_command`. The deploy workflow deploys the worker
  first, so a failed migration stops the run before the ingest app changes.
- **The ingest app** logs in as `gaslit_ingest`, a member of `ingest_writer` with no rights
  of its own. It no longer builds totals or runs upkeep; it still marks late summaries in
  `totals_stale`, which `ingest_writer` may insert into. M4's dashboard logs in the same way
  as `gaslit_dashboard`, a member of `dashboard_app`.
- **Login roles are made by `python -m app.db.login_role {ingest,dashboard}`**, run on the
  worker over `fly ssh console`. It creates the role with SQL (console roles would join
  `neon_superuser`, which bypasses RLS), generates a 43-character password, and prints the
  app's `DATABASE_URL` (the owner's URL with that login), piped into `fly secrets import`, so
  the password is never shown. Running it again sets a new password.
- **Tested:** the end-to-end test runs the whole ingest app as `gaslit_ingest` with
  passwords required, so every query ingest makes must be allowed to `ingest_writer`. The
  login cannot read data or users' details, drop a table or create a partition.

## Trade-offs
- **About $2 a month** for the always-running worker (`shared-cpu-1x`, 256 MB), where a
  scheduled machine would cost cents. In return, deploys stay one `fly deploy` per app, and
  retention runs on time even while no SDK sends (under ADR 0020 it waited for the next
  start). M5's status poller belongs in the worker too (brief §9.8).
- **The database wakes every hour** for about 5 minutes: ~15 compute-hours a month of Neon
  Free's 100, or about $1.60 a month on the paid plan. While SDKs are sending it is awake
  anyway.
- **The newest hour of a dashboard read comes from per-minute summaries**, where totals were
  at most a few minutes behind. Reads stay correct (ADR 0023's watermark); a busy account's
  1-hour read from summaries took 6 ms.
- **Two builds per deploy**, one per app, because each deploy token is scoped to its own app.
  The builder's layer cache makes the second quick. Reusing one image across apps would need
  a token for both.
- Owner rights are still on one Fly machine. A compromised worker reads everything; it serves
  no requests, so it is much harder to reach than ingest.
- Alerts: the worker's `gaslit_partitions_last_success_timestamp_seconds` replaces the
  totals-build counters, since the build is part of each upkeep run.
