# 0019: Accounts over quota are answered from memory

- **Status:** accepted
- **Date:** 2026-10-18

## Context
[ADR 0017](0017-rate-limit-and-quota.md) answers an account over quota with `429` and
`Retry-After: 60`, so its SDKs check back every minute. The key cache also lasts 60 s
([ADR 0015](0015-ingest-key-format-and-lookup.md)), so almost every check missed the cache and
ran the key lookup, an `UPDATE` on Neon. If blocked accounts are the only traffic, Neon never
sleeps. On the paid plan that is the full ~$19.35 a month of 0.25 CU. On Neon Free the 100
CU-hours run out in about 17 days, and then the database stops for everyone.

Neon suspends a compute after 5 minutes with no active queries (fixed on Free; checked
2026-10-18, [scale to zero](https://neon.com/docs/introduction/scale-to-zero)). Open idle
connections do not count as activity
([compute lifecycle](https://neon.com/docs/introduction/compute-lifecycle)).

A second gap: a machine that has just started learns that an account is over only at its
first flush for it, so it accepts one batch first.

Saad agreed to the fix proposed in the handoff on 2026-10-18.

## Options considered
1. **Check blocked accounts against the database less often** (say every 15 or 60 minutes).
   Each check wakes Neon for at least 5 minutes. Every 15 minutes keeps it up about a third of
   the time (~$6 a month); every hour, about $1.60, but an upgrade then waits up to an hour.
2. **Answer blocked accounts from memory until the month ends.** No database work at all
   while the account is blocked. The catch: a plan change in the database is no longer seen,
   so the upgrade flow must tell ingest.
3. **Listen for plan changes** (`LISTEN`/`NOTIFY`). A listening connection keeps Neon awake.

## Decision
Option 2.
- **The key store keeps blocked keys past their expiry.** `KeyStore(pool, keep=quota.blocks,
  learn=quota.learn)`. When a cached key has expired, it is still served while `keep(key)` is
  true, that is while the key's account is over quota for this month under the cached plan.
  The request then gets its `429` from memory. Once the month ends, `blocks` turns false and
  the next request looks the key up again.
- **The key lookup also returns this month's usage.** The same single `UPDATE ... RETURNING`
  left-joins `monthly_usage` for the current UTC month (`ingest_writer` could already read it)
  and passes the totals to `Quota.learn`. A machine that has just started blocks an account at
  its first request, before reading the body.
- **`learn` keeps the larger total for the same month.** Totals only grow within a month, so
  when a lookup and a flush return out of order, the older answer never lowers the count.
  Flushes use `learn` too.
- **`Quota.unblock(user_id)` for the upgrade flow.** It forgets the account's totals, so the
  next request looks the key up, gets the new plan and its usage, and is checked against the
  new limits. Sending then resumes at the SDK's next check, within a minute. The flow (M4 or
  the paid tier) must call it on every ingest machine. While there is one app, that is a plain
  function call.
- `Retry-After` stays at 60 s.

## Trade-offs
- **A plan change made only in the database does not unblock an account** until the month
  ends or the machine restarts (a restart relearns usage and the new plan). No upgrade flow
  exists yet, and there is no paid plan, so nothing is lost today.
- **A blocked account's revoked key gets `429`, not `401`,** until the month ends, and the same
  goes for a deleted project's keys. They are refused either way; the SDK just keeps checking
  once a minute instead of stopping. The upgrade flow's `unblock` could later serve
  revocation too.
- **Memory has to survive.** If the Fly machine stops, the next request starts a new one,
  which looks the key up once (waking Neon for 5 minutes) and blocks again. Fly's proxy stops
  a lone machine "if the Machine has no traffic (a load of 0)" when it checks, every few
  minutes ([Fly docs](https://docs.fly.io/reference/fly-proxy-autostop-autostart), checked
  2026-10-18). The docs do not say whether one request a minute counts as traffic. If it does
  not, the machine could restart every few minutes and keep Neon awake after all. Two ways
  to make it certain, for the hosting step: `min_machines_running = 1` (one machine always
  on, about $2 a month), or `auto_stop_machines = "suspend"`, which keeps the machine's memory
  while it is stopped. Measure which one is needed once it is hosted.
- With several ingest machines, each one blocks the account the first time it sees it, after
  one lookup.
