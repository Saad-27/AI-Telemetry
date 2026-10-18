# 0013: First schema: day partitions, group roles and row-level security

- **Status:** accepted; how partitions are made in production: [0020](0020-partition-upkeep-and-retention.md)
- **Date:** 2026-10-17

## Context
M3 starts with the database (brief §9.4, §11.6, §14). The brief fixes the outline: plain
Postgres (D3), append-only `summary_records` and `call_samples` partitioned by day, Alembic for
every schema change, least-privilege roles and RLS on tenant tables (D10). It leaves open how
partitions are made, how the roles work on Neon, and some column choices.

Facts checked on 2026-10-17: Postgres `COPY FROM` refuses tables with RLS for roles subject to
it (the tests confirm it). On Neon, roles made in the console are members of `neon_superuser`,
which has `BYPASSRLS`; roles made with SQL are not, and `NOLOGIN` roles are supported
([Neon: manage roles](https://neon.com/docs/manage/roles)). SQLAlchemy 2.1 no longer installs
`greenlet`, which its async mode needs.

## Options considered
**Partition creation**
1. pg_partman: well known, but an extension to install and check on Neon.
2. A small function, `app.db.partitions.ensure_partitions`, run as the schema owner.

**Batch inserts**
1. `COPY` (brief §21): fastest, but refused under RLS and has no `ON CONFLICT`.
2. `COPY` into a temporary table, then `INSERT ... SELECT ... ON CONFLICT DO NOTHING`.
3. One `INSERT ... SELECT FROM unnest($1, $2, ...) ON CONFLICT DO NOTHING`: one round trip.

**How Alembic connects**
1. psycopg, a second Postgres driver.
2. SQLAlchemy's asyncio mode over asyncpg (adds `greenlet`).

## Decision
- **Tables** (migration `0001`): `users`, `projects`, `project_members`, `ingest_keys`,
  `summary_records`, `call_samples`. Sessions, OAuth identities and the audit log come with M4.
  Migrations are plain SQL; there are no ORM models.
- **Partitions:** one per UTC day, made by `ensure_partitions` for yesterday through 7 days ahead
  (`python -m app.db.partitions`, `make migrate`). How it runs in production is decided with the
  retention job (M3 step 5); a missed run is harmless for a week. No default partition: a row
  for a day with no partition is an error, and ingest validation keeps timestamps near now.
- **Ingest will batch with option 3** (`unnest`), replacing `COPY`. Measured in the load test.
- **Roles:** the migration creates `NOLOGIN` group roles `ingest_writer` and `dashboard_app`.
  - `ingest_writer` can look up keys by hash, set `last_used_at` and insert data. It cannot
    read data, users or key prefixes.
  - `dashboard_app` can read data, and only for the project set with
    `SET LOCAL app.project_id` (RLS). Its grants grow with the M4 routes.
  - Login roles are created with SQL at hosting time and granted membership, never in the
    console, so they lack `BYPASSRLS`. The `worker` and `migrator` roles in §11.6 are the schema
    owner for now: dropping partitions needs ownership.
- **Columns:**
  - One count column per error class (`err_rate_limited`, ...), so sums are plain `SUM()`.
    Total errors are `count - ok - cancelled`.
  - Histograms are `integer[]`.
  - No foreign key from data tables to `projects`: the project was resolved from the key, and
    deletion is a job, not a cascade across partitions.
  - The primary keys include the partition key (Postgres requires it), which also gives the
    idempotent `(project_id, minute, summary_id)` and `(project_id, ts, event_id)`.
  - `(project_id, ts, event_id)` serves the live feed read backwards, so the separate
    `(project_id, ts DESC)` index in §9.4 is not created.
  - `duration_ms` and `ttft_ms` are `real` (4 bytes): 0.1 ms precision up to ~1,000 s.
- **Not stored:** `share_global` and `origin_continent` (global baseline, dropped in D6) and
  `model_key` (its use was the global comparison). Each is a cheap column to add later.
- **Alembic over asyncpg** (option 2), with the DSN passed to asyncpg unchanged so it is parsed
  exactly as the app parses it.

## Trade-offs
- `INSERT ... unnest` is slower than `COPY`. If the load test shows it matters, use option 2.
- RLS only protects connections that use `dashboard_app`. Until the app connects with separate
  login roles (hosting step and M4), the owner connection bypasses it, as owners do.
- Alembic, SQLAlchemy and greenlet add ~33 MB to the image (160 to 193 MB). The server never
  imports them, only the release step, so memory is unchanged.
- Partition DDL briefly locks the parent table. It runs daily and creates at most two tables.
