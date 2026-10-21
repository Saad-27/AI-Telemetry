# 0020: Partition upkeep and retention run inside the app, once a day

- **Status:** accepted. Retention per table, and building totals before dropping: [0023](0023-tiered-totals.md)
  Where it runs (the worker app, hourly) is replaced by [0026](0026-worker-app-and-login-roles.md).
- **Date:** 2026-10-18

## Context
`summary_records` and `call_samples` have one partition per UTC day, and there is no default
partition, so a day's partition must exist before its rows arrive
([ADR 0013](0013-schema-partitions-and-rls.md)). Free accounts keep 7 days (brief §7.2), and
retention works by dropping whole days (§9.4, §11.6). ADR 0013 left open how this runs in
production. The brief's `worker` app (§9.8) does not exist: there is one Fly machine, which
scales to zero ([ADR 0007](0007-neon-and-scale-to-zero.md)). Neon suspends after 5 minutes with
no queries, so whatever runs the job should not wake it often.

Checked on Postgres 17 (2026-10-18):
- Creating or dropping a partition takes an `ACCESS EXCLUSIVE` lock on the parent table, so
  inserts queue behind it.
- A `CREATE TABLE IF NOT EXISTS` for a partition that exists takes no lock.
- `IF NOT EXISTS` fails if another session creates the same table at the same moment.
- Only the parent table's owner can create a partition.

## Options considered
1. **In the app:** at startup, then whenever a new UTC day begins. No new component.
2. **A Fly scheduled machine** (`--schedule daily`) running `python -m app.db.partitions`. It
   runs even while the app is stopped, but it is one more machine to create, keep on the
   current image and watch.
3. **pg_partman with pg_cron.** These are extensions to check on Neon; ADR 0013 already chose
   a small function over pg_partman.
4. **A GitHub Actions schedule.** The owner's credentials would live in GitHub, and scheduled
   runs can be delayed or switched off.

## Decision
Option 1.
- **`app.db.partitions.maintain(conn, today, keep_days)`** creates the partitions for yesterday
  through 7 days ahead, then drops expired days. It runs under an advisory lock
  (`pg_try_advisory_lock`), so with several machines only one runs the DDL; the others skip.
  Each statement commits on its own, so a parent table is locked only for its own statement,
  and `lock_timeout = 1s` keeps a queue of inserts from growing behind a lock it cannot get.
- **What is dropped:** on day D, the partitions for days before D − `keep_days`. Every row in
  them is at least `keep_days` old, so rows are kept for between 7 and 8 days. Only partitions
  named like ours (`<table>_pYYYYMMDD`) are dropped, and the name is rebuilt from the date.
- **`keep_days` is `longest_retention_days()`** (`app/core/entitlements.py`): the longest
  retention of any plan, because every account's rows share each day's partition.
- **`maintain_daily`** runs in the lifespan as a background task, so the app still starts
  while the database is down. It runs `maintain` at startup, then checks every hour whether a
  new UTC day has begun, so it runs at most once a day per machine. A failed run is logged
  (exception type only) and retried at the next check.
- `ensure_partitions` ignores a partition made by another session at the same moment.
- `python -m app.db.partitions` (and `make migrate`) runs the same `maintain`.

## Trade-offs
- **It runs only while a machine runs.** If no traffic at all reaches the app, Fly stops it,
  and expired days wait for the next start, which drops them at once. Partitions are made a
  week ahead, so creation is never late. If the retention promise needs to hold during long
  idle spells, use `min_machines_running = 1` (about $2 a month, see ADR 0019) or option 2.
- **It wakes an idle database once a day:** about 5 minutes at 0.25 CU, or 0.6 CU-hours a
  month (about $0.06 on the paid plan, 0.6% of Neon Free's 100 hours).
- **It needs the table owner's rights.** Today the app connects as the owner. At hosting
  (step 8), when the app connects as a member of `ingest_writer`, the job needs another way
  in: a second, owner connection string used only by the job, or a `SECURITY DEFINER` function
  with no arguments that the app's role may call. Decide this together with where migrations
  run, since they need the owner too.
- **One retention for every plan.** A plan that keeps fewer days than the longest would need
  its rows deleted (paid tier).
- If every run fails for a week, the partitions run out and ingest returns `503` for everyone.
  The metrics (step 5) record when the last run succeeded, so an alert can catch it first.
- A creation or drop that cannot get its lock within 1 s fails and is retried within the hour.
