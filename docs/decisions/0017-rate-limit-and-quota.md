# 0017: Per-key rate limit and per-account monthly quota

- **Status:** accepted
- **Date:** 2026-10-17

## Context
Brief §7.2 gives free accounts 1M calls measured a month (D7). Over quota, ingest returns
`429` with `Retry-After` until the quota resets. §11.4 gives each key a request rate limit,
and §19.4 says user-facing limits count calls (the sum of the summaries' `count`), while
protective limits apply to records and requests. §20.7 says limits come from `entitlements()`,
and ADR 0008 says quotas are counted in the database, so several machines agree.

Things that shape the design:
- Summaries are kept for 7 days, so a monthly count cannot be summed from them.
- The SDK (ADR 0004) waits `Retry-After` (capped at 300 s) after a 429, then retries the same
  batch. It drains a backlog back-to-back in batches of up to 200 records.
- Ingest makes no database call per request today, apart from the cached key lookup.
- Samples count no calls, so a calls quota alone does not stop a flood of samples.

## Options considered
**Counting the quota**
1. **Upsert a counter on every request.** Exact, even across machines. But it adds a round
   trip and a write to one hot row per account on every request (brief §19.4: no updates on
   the hot path), and ties ingest to the database per request, which stage 2's stream sink
   (§20.3) is meant to remove.
2. **Count in memory, add to the database every 10 s.** One statement per flush for all
   accounts. The flush returns each account's stored total, including other machines' counts.

**Rate limit**
1. **Requests per key.** A request can carry 1,000 records, so this allows up to 1,000 times
   as many rows.
2. **Records per key, as a token bucket.** Every request costs at least one token.

## Decision
- **`entitlements(plan)`** (`app/core/entitlements.py`) returns `projects`, `retention_days`,
  `monthly_calls`, `records_per_s` and `records_burst`. Free: 1 project, 7 days, 1M calls a
  month, 50 records/s per key with bursts to 3,000. An unknown plan gets the free limits.
  - The numbers are placeholders. 50 records/s covers about 25 SDK processes at their worst
    (every error sampled), and much more in normal use. 3,000 is 15 full SDK batches.
- **The key lookup also returns the owner's id and plan** (one join, still one round trip, still
  cached for 60 s). `ingest_writer` gets column grants only: `projects (id, owner_user_id,
  deleted_at)` and `users (id, plan)`. Since it can now see `deleted_at`, keys of deleted
  projects are refused too.
- **Order:** after the key, before reading the body: quota, then rate limit. Both are checked
  from memory. `429 {"error": "quota_exceeded"}` with `Retry-After` until the next UTC month,
  or `429 {"error": "rate_limited"}` with `Retry-After` until the bucket is back to one token.
  `Retry-After` is in whole seconds, rounded up.
- **Rate limit: option 2, per machine, in memory** (`RateLimiter`). The request takes one token
  up front, then its records are charged once the envelope is parsed, which may leave the
  bucket in debt. A rejected request costs nothing. A batch over 1,000 records (413) is not
  charged for its records. Buckets are kept for at most 10,000 keys, least recently used out.
- **Quota: option 2** (`Quota`). After a successful insert, the batch's calls are added in
  memory per account and UTC month. Every 10 s, one `INSERT ... SELECT FROM unnest() ON CONFLICT
  DO UPDATE ... RETURNING` adds them to `monthly_usage (user_id, month, calls)`, in sorted order
  so two machines cannot deadlock. No query when nothing is pending, so an idle Neon database
  can sleep. A failed flush keeps its counts for the next one. The app flushes once more on
  shutdown, which is how Fly stops idle machines.
- **An account is over** when its stored total at the last flush, plus calls not yet flushed,
  reaches `monthly_calls`. Totals only grow within a month, so an old total never blocks too
  early; a new month or a higher limit (within 60 s, through the key cache) unblocks it.
- `monthly_usage` has no RLS: it is per account, not per project. The dashboard reads it by the
  signed-in user's id for the over-quota banner (M4).

## Trade-offs
- An account can go over by up to 10 s of its traffic per machine, plus requests in flight. A
  newly started machine learns an account is over at its first flush for that account.
- A machine that crashes loses up to 10 s of counts. A flush that times out after committing
  is retried and counted twice. Replayed batches are counted again, like `accepted` (ADR 0016).
- With several machines, each has its own buckets, so a key's rate limit is per machine.
  Cloudflare's per-IP limit still applies in front.
- A leaked key can use up its account's quota. The brief's answer is revoking the key (§11.1).
- The per-IP limit on invalid keys (§11.5) is not built. It needs a client IP the app can
  trust, which depends on closing the Cloudflare bypass at step 8 (`docs/security-config.md`).
  Until then, made-up keys with a valid checksum cost one indexed lookup each, and Cloudflare's
  per-IP limit caps them.
- The checks add about 4 µs per request (measured), against ~0.5 ms for a typical batch.
