# 0022: Dashboard reads: one sum per histogram bucket, behind a repository

- **Status:** accepted. The latency target and the 7-day fix below are **proposed**, for Saad
- **Date:** 2026-10-18

## Context
ADR 0008 has the dashboard sum `summary_records` when a page loads, with no rollup worker.
Brief §19.4 leaves the method open: Postgres has no element-wise array sum, so histograms need
one of a custom aggregate, `unnest ... WITH ORDINALITY`, or summing in Python. It says to pick
the simplest that performs well and record it here. Brief §20.7 asks for a repository layer
for metric queries. Saad wants the dashboard fast to load and to change (HANDOFF, "Dashboard
direction"), so this step also proposes a latency target.

**Measurements** (2026-10-18). Postgres 17.11 in Docker on WSL2, default settings (128 MB
shared buffers), warm cache, one core per query (Neon's 0.25 CU has less than one). The table
holds 7 days of summaries, 1,008,000 rows and 624 MB, written minute by minute in batches
as ingest writes them, so each project's rows are spread among everyone else's. The busy
project has 120,960 summaries (12 a minute, such as 4 processes × 3 series); the typical one
has 20,160 (2 a minute). Each query returns the overview: the counts and all three
histograms per model and route. Times are medians in ms.

| Method, busy project | 1 h | 24 h | 7 d |
|---|---|---|---|
| **One `sum()` per bucket**: `ARRAY[sum(ttft_hist[1]), ...]` | 4 | 88 | 637 |
| `unnest ... WITH ORDINALITY`, grouped by position | 22 | 485 | 3,336 |
| An aggregate written in SQL | 61 | 1,481 | 12,304 |
| Rows fetched and summed in Python | 8 | 176 | 1,363 |

Two variants of the first gave little: copying each array once per row (no change), and one
column per bucket instead of arrays (−35%, at the cost of a schema change). JIT made no
difference. The cost is about 5 µs per summary in the range: reading the row (~100 ms of the
637) and its 66 sums.

Through the repository, as `dashboard_app` under RLS (adds ~2 ms):

| Query | 1 h | 24 h | 7 d |
|---|---|---|---|
| Overview, busy project | 6 | 93 | 651 |
| Chart series of one model, busy project (1 min, 5 min, 1 h points) | 9 | 60 | 363 |
| Overview, typical project | 2 | 14 | 107 |

## Decision
- **One `sum()` per bucket**, generated from the bucket counts in `app.ingest.schema`. It is
  plain SQL, needs no function or extension, and was 5 times faster than the next method.
- **`app.dashboard.repository.MetricsRepository`** is the only way routes read metrics:
  - `overview(project, start, end, filters)`: totals per provider, route and model.
  - `series(project, start, end, step, filters)`: totals per step and input bucket. Steps are
    counted from midnight UTC, so 1 minute, 5 minutes and 1 hour fall on round times.
  - `Filters`: provider, route, model, service, environment, origin region; `None` matches
    anything. Each is a parameter (`$n IS NULL OR column = $n`), so each query is one fixed
    statement. No index is needed for them: the primary key's `(project_id, minute)` finds the
    rows, and the filters drop some of them.
  - Ranges are `[start, end)` on `minute`, the minute's start.
  - Results use the summary record's names (`Sums`), so callers and tests compare them with
    payloads directly.
- **Tenancy:** each read runs in a read-only transaction that first sets `app.project_id` with
  `set_config(..., true)` (the same as `SET LOCAL`), so RLS limits `dashboard_app` to that
  project (ADR 0013) even if a `WHERE` clause were wrong. The `WHERE project_id = $1` is still
  there: it is what uses the index, and the owner connection used until step 8 skips RLS.
- **Percentiles:** `quantile(hist, bounds, q)` interpolates linearly inside the bucket, so it
  is off by at most that bucket's width. Buckets include their upper bound (ADR 0003) and the
  first starts at 0. In the +inf bucket it returns the last bound ("at least 120 s"), as
  Prometheus does. Under 20 values it returns `None` (brief §10.3).
- **Metric:** `gaslit_dashboard_query_seconds{query="overview"|"series"}`, the signal ADR 0008
  watches instead of rollup lag. The histogram bounds moved into `app.ingest.schema` as
  `LATENCY_BOUNDS_MS` and `DECODE_BOUNDS_TPS`, next to the bucket counts.
- **End-to-end test** (`tests/test_end_to_end.py`): every example sends to the real app over
  HTTP; every printed record must be stored, and the repository's sums must equal the
  payloads' sums, by model and by minute. It also compares the server's provider and route
  enums, host allowlist and histogram bounds with the SDK's own tables.

## Proposed latency target (Saad to confirm)
A range or filter change should feel instant. Proposed: **each dashboard query under 100 ms at
p95 on the server**, for any range, for the busiest account the load test models, on the
production database size (0.25 CU). ADR 0008's 300 ms remains the point where pre-summed data
must be added. Step 7 measures it.

## Open: 7-day reads for busy accounts (Saad to decide)
Read-time sums meet the target for 1 hour and 24 hours. They miss it for 7 days for any
account that sends summaries most minutes: 107 ms typical and ~650 ms busy, locally; with less
than one core, Neon is likely slower. The brief's `rollups_1m` would not fix it: a 7-day chart
would still read one row per minute per series.

Proposed fix: **hourly totals for hours older than 24 hours.** Ingest refuses summaries more than
24 hours old (ADR 0016), so those hours can never change: totals made once are exact
forever, with no worker and no recomputation. A 7-day read then sums the hourly totals plus the
raw summaries of the last day. Measured: **68 ms instead of 620 ms** for the busy project.
Building the totals took 8 s for 6 days of the whole test table, about 1.4 s a day.
When to build them:
1. In the daily partition job (ADR 0020), which already wakes the database once a day. No
   extra cost. The raw part is then 24 to 48 hours: about 70 ms for the busy project just
   after the job, roughly twice that just before the next run.
2. Every hour, keeping the raw part under 25 hours, but waking Neon each hour. Neon stays up
   5 minutes after the last query ([Neon](https://neon.com/docs/introduction/scale-to-zero),
   checked 2026-10-18), so up to about $1.60 a month more (24 × 5 minutes a day at 0.25 CU,
   $0.106 per CU-hour) when nothing else keeps it awake.
3. Not yet: wait for the step 7 numbers on Neon.

## Trade-offs
- Read cost grows with the summaries in the range: ~5 µs each. 1-hour and 24-hour reads stay
  fast; 7-day reads of busy accounts need the fix above.
- 54 sums in every query make the SQL long. It is generated from constants, and a test
  compares the result with hand-added payloads.
- `None` filters cannot select "no service": a filter for missing tags would need its own
  parameter. Not needed until the dashboard offers it.
- One column per bucket would be 35% faster but changes the schema and the ingest insert. Not
  worth it alone; reconsider if the fix above is declined.
