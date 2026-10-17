# 0017: Per-account rate limit and monthly quota, sized to the database bill

- **Status:** accepted
- **Date:** 2026-10-17

## Context
Brief §7.2 gives free accounts 1M calls measured a month (D7). Over quota, ingest returns
`429` with `Retry-After` until the quota resets. §11.4 gives each key a request rate limit,
and §19.4 says user-facing limits count calls (the sum of the summaries' `count`), while
protective limits apply to records and requests. §20.7 says limits come from `entitlements()`,
and ADR 0008 says quotas are counted in the database, so several machines agree.

Saad pays for the database, so free accounts must fit in its bill. Neon's paid plan, checked on
2026-10-17 ([pricing](https://neon.com/pricing), [plans](https://neon.com/docs/introduction/plans)):
- $0.106 per CU-hour, with no monthly minimum.
- $0.35 per GB-month of storage.
- $0.20 per GB-month of restore history, with a 1-day window by default.

Two costs follow:
- **Compute.** It runs all day once anyone sends data all day. Fixed at 0.25 CU, it costs at
  most about $19.35 a month, whatever the traffic (`docs/deploy.md` step 2). The limits do not
  change it.
- **Storage.** This is what the limits must bound. Measured on our schema, including indexes,
  a summary takes 639 bytes and a sample 356.

Things that shape the design:
- Summaries are kept for 7 days, so a monthly count cannot be summed from them.
- Samples count no calls, so a calls quota alone does not stop a flood of samples.
- The SDK sends at most two records per call: every call is in one summary, which has
  `count` ≥ 1, and in at most one sample.
- A project can have any number of keys, so a limit per key can be multiplied.
- The SDK (ADR 0004) waits `Retry-After` (capped at 300 s) after a 429, then retries the same
  batch. It drains a backlog back-to-back in batches of up to 200 records.
- Ingest makes no database call per request today, apart from the cached key lookup.

## Options considered
**Counting the quota**
1. **Upsert a counter on every request.** Exact, even across machines. But it adds a round
   trip and a write to one hot row per account on every request (brief §19.4: no updates on
   the hot path), and ties ingest to the database per request, which stage 2's stream sink
   (§20.3) is meant to remove.
2. **Count in memory, add to the database every 10 s.** One statement per flush for all
   accounts. The flush returns each account's stored totals, including other machines' counts.

**Bounding storage**
1. **Rate limit only.** Storage kept is the rate × 7 days: 50 records/s allows ~15 GB per key
   (~$5 a month), and more with more keys. A rate low enough to bound storage would block real
   bursts.
2. **A monthly cap on records stored, per account,** next to the calls quota. The rate limit
   then only protects compute from bursts.

**Rate limit:** per key (§11.4, but keys are free to create) or per account.

## Decision
- **`entitlements(plan)`** (`app/core/entitlements.py`) returns `projects`, `retention_days`,
  `monthly_calls`, `monthly_records`, `records_per_s` and `records_burst`. An unknown plan
  gets the free limits.
- **The free plan:** 1 project and 7 days, plus these limits (placeholders, revisited after the
  load test):

  | Limit | Value | Why |
  |---|---|---|
  | Calls a month | 1M | D7 |
  | Records a month | 2M | Twice the calls, so a real SDK reaches the calls quota first |
  | Rate | 20 records/s | Protects compute from bursts. At this rate, an abuser reaches the records cap in about a day |
  | Burst | 3,000 records | 15 full SDK batches, for a backlog after an outage |

- **What one free account can cost in storage.** Neon bills the average stored over the month,
  and each record is kept for 7 days:

  | Account | Stored, on average | Per month |
  |---|---|---|
  | Typical, at 1M calls (brief §7.2: ~30k records) | ~15 MB | ~$0.005 |
  | Worst real SDK pattern, at 1M calls | ~230 MB | ~$0.08 |
  | Abuse, at the records cap (up to ~1.3 GB for a week) | ~300 MB | ~$0.10, plus restore history |

  With compute fixed, 100 typical free accounts cost about $19.35 plus $0.50 a month.
- **The key lookup also returns the owner's id and plan** (one join, still one round trip, still
  cached for 60 s). `ingest_writer` gets column grants only: `projects (id, owner_user_id,
  deleted_at)` and `users (id, plan)`. Since it can now see `deleted_at`, keys of deleted
  projects are refused too.
- **Order:** after the key, before reading the body: quota, then rate limit. Both are checked
  from memory. `429 {"error": "quota_exceeded"}` with `Retry-After` until the next UTC month,
  or `429 {"error": "rate_limited"}` with `Retry-After` until the bucket is back to one token.
  `Retry-After` is in whole seconds, rounded up.
- **Rate limit: per account, per machine, in memory** (`RateLimiter`), as a token bucket in
  records. The request takes one token up front, then its records are charged once the
  envelope is parsed, which may leave the bucket in debt. A rejected request costs nothing. A
  batch over 1,000 records (413) is not charged for its records. Buckets are kept for at most
  10,000 accounts, least recently used out.
- **Quota: option 2** (`Quota`). After a successful insert, the batch's calls and accepted
  records are added in memory per account and UTC month. Every 10 s, one `INSERT ... SELECT
  FROM unnest() ON CONFLICT DO UPDATE ... RETURNING` adds them to `monthly_usage (user_id,
  month, calls, records)`, in sorted order so two machines cannot deadlock. No query when
  nothing is pending, so an idle Neon database can sleep. A failed flush keeps its counts for
  the next one. The app flushes once more on shutdown, which is how Fly stops idle machines.
- **An account is over** when a stored total at the last flush, plus counts not yet flushed,
  reaches its limit. Totals only grow within a month, so an old total never blocks too early;
  a new month or a higher limit (within 60 s, through the key cache) unblocks it.
- `monthly_usage` has no RLS: it is per account, not per project. The dashboard reads it by the
  signed-in user's id for the over-quota banner (M4).

## Trade-offs
- An account can go over by up to 10 s of its traffic per machine, plus requests in flight. A
  newly started machine learns an account is over at its first flush for that account.
- A machine that crashes loses up to 10 s of counts. A flush that times out after committing
  is retried and counted twice. Replayed batches are counted again, like `accepted` (ADR 0016).
- With several machines, each has its own buckets, so the rate limit is per machine. Monthly
  caps are shared through the database. Cloudflare's per-IP limit still applies in front.
- A leaked key can use up its account's quota. The brief's answer is revoking the key (§11.1).
- Someone opening many accounts could cost about $0.10 a month each. Sign-up abuse is an M4
  question.
- Not verified: whether Neon counts indexes in storage (the sizes above include them, so they
  are the high end), and how much restore history our writes produce.
- The per-IP limit on invalid keys (§11.5) is not built. It needs a client IP the app can
  trust, which depends on closing the Cloudflare bypass at step 8 (`docs/security-config.md`).
  Until then, made-up keys with a valid checksum cost one indexed lookup each, and Cloudflare's
  per-IP limit caps them.
- The checks add about 4 µs per request (measured), against ~0.5 ms for a typical batch.
