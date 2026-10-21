# 0023: 5-minute and hourly totals, built while data arrives

- **Status:** accepted (Saad, 2026-10-18). Replaces ADR 0008's "sum at read time, no rollups"
  and the brief's every-minute `rollups_1m` worker (§9.5).
  "Who builds" is replaced by [0026](0026-worker-app-and-login-roles.md): the worker, hourly.
- **Date:** 2026-10-18

## Context
ADR 0022 measured read-time sums at about 5 µs per summary in the range. A 7-day read of a busy
account took ~650 ms, against a proposed target of 100 ms. Saad: detail older than 24 hours can
be coarser, as long as totals remain, but every range should be fast.

The brief's `rollups_1m` would not help: a 7-day chart would still read one row per minute per
series. And ADR 0008 avoided a worker that wakes Neon every minute.

## Options considered
1. **Hourly totals only for hours over 24 h old.** Those hours can never change, since ingest
   refuses older summaries (ADR 0016). Fixes 7 days (68 ms), not 24 hours (~90 ms, busy).
2. **Totals per 5 minutes and per hour, kept current.** The usual time-series design
   (downsampling): each view reads the size of totals that matches its points. Needs late
   data handled, since recent buckets can still change.
3. **Response caching, bigger database, one column per bucket:** each helps a little, none
   bounds the cost.

## Decision
Option 2.
- **Tables** (migration `0003`): `totals_5m` and `totals_1h`, one row per project, bucket and
  series, with every series column, so every filter still applies. Day partitions like the
  summaries'; RLS for `dashboard_app` like the summaries'. Counts and histograms are `bigint`:
  a bucket sums many summaries, and an overflow would stop every account's build.
- **Building** (`app.db.totals.build`): each 5-minute bucket is built 3 minutes after it ends,
  for all projects in one statement; an hour is built from its twelve buckets. A build
  replaces whole buckets (delete, then insert), so running it twice changes nothing. Measured:
  16 ms per bucket for every account at once (on the test data below).
- **Who builds:** whichever machine is receiving summaries checks every minute
  (`app.db.totals.Totals`). With no traffic it never touches the database, so Neon still
  sleeps. The daily upkeep (ADR 0020) also builds, before it drops anything. An advisory lock
  makes one machine build at a time.
- **Where totals end:** `totals_watermark.built_through`. Reads use totals before it and
  summaries after it, so a read is correct whether or not a build has run, only slower.
- **Late summaries:** a summary that arrives 2 minutes or more after its bucket ended may
  miss the build. Ingest then marks its project and bucket in `totals_stale`, right after
  storing it. The next build rebuilds that project's bucket and hour from the summaries. Late
  data shows within about a minute. The mark is written only for late batches, so ingest's
  normal path is unchanged.
- **Reads** (`MetricsRepository`): each range is split into at most five pieces: summaries,
  5-minute totals, hourly totals, 5-minute totals, summaries. The coarsest totals that fit
  are used, and summaries cover the edges and the newest minutes. Chart steps use only totals
  that divide them: 1-minute charts read summaries, 5-minute charts 5-minute totals. It is one
  fixed statement; unused pieces are empty ranges.
- **Retention:** per-minute summaries and 5-minute totals are kept for today and the two days
  before (48-72 hours): longer than the 24 hours in which late data can arrive, and enough for
  the 24-hour view. Hourly totals and samples keep the plan's 7 days. Summaries not yet in
  totals are never dropped. A range starting more than 2 days ago starts at its hour.
- **Long pauses:** a build that would start more than a day back (every machine stopped) is
  left to the daily upkeep, which first makes the totals partitions for those days.
- **Metrics:** `gaslit_totals_builds_total{result="ok"|"locked"|"failed"}` and
  `gaslit_totals_build_seconds`.

**Measured** (2026-10-18), on the ADR 0022 test data (7 days, 1M summaries, busy project 121k).
Local, warm cache, one core. Median ms, as `dashboard_app`:

| | 1 h | 24 h | 7 d |
|---|---|---|---|
| Busy, overview (before: read-time sums) | 5 (6) | 7 (93) | 10 (651) |
| Busy, one model's chart: 1-minute, 5-minute, hourly points (before) | 11 (9) | 29 (60) | 18 (363) |
| Typical, overview (before) | 5 (2) | 5 (14) | 7 (107) |

The fixed cost of a read rose ~3 ms (watermark lookup, five pieces). Storage for the test data
fell from 624 MB to 308 MB (237 MB summaries, 58 MB 5-minute, 13 MB hourly).

## Trade-offs
- **Detail older than 2 days is hourly only.** A 5-minute chart cannot reach further back.
  Saad agreed to this.
- **Late data appears after the next build** (within about a minute), not at once.
- **More moving parts:** three tables, a builder, a mark in ingest. Every path is tested
  against summing the summaries directly: from totals, from summaries, and end to end.
- **The builder runs as the schema owner** (it reads every project's summaries), like
  partition upkeep. Its role is a step 8 decision, with the upkeep's.
- **A machine stopped right after its last batch** leaves its newest bucket unbuilt. Reads
  then use summaries for it (still correct), until any machine next builds.
- Revisit for 30- or 90-day views (paid tier): add daily totals the same way.
