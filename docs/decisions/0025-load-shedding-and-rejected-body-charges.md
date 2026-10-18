# 0025: Shed load with 429, and charge rejected bodies by size

- **Status:** accepted (Saad, 2026-10-18)
- **Date:** 2026-10-18

## Context
The step 7 load test (`docs/capacity.md`, ADR 0024) found two weaknesses in ingest.

**1. Overload collapsed.** Past about 200 req/s, the database (0.25 CPU) fell behind. Requests
queued for the 5-connection pool, and clients gave up at 5 s while the app went on serving
their abandoned requests. At 300 req/s almost nothing was stored. Brief §11.5 asks for "bounded
concurrency ... instead of queueing without limit".

**2. Junk was cheap to send and dear to answer.** Any account could send it with its key:
- A zip bomb (5.8 KB sent, inflated to the 5 MB cap) cost 11 ms of CPU, 5× a normal request.
- 100 junk bodies a second used 0.75 of a core and slowed real traffic to a p95 of 850 ms.
- The rate limiter charged each one token, so one free account could keep a `shared-cpu-1x`
  machine's quota busy.

The SDK never sends junk: its largest batch (1,000 records) is about 0.7 MB.

Saad also decided (2026-10-18):
- Stay on `shared-cpu-1x` until there are many users.
- **No per-request limit per account.** Legitimate use should never be throttled by how it is
  spread over processes. The existing bucket already charges each request a token, which caps
  an account's tiny requests at about 10 a second.

## Options considered
**Shedding**
1. uvicorn's `--limit-concurrency`. It counts connections as well as tasks, so the idle
   keep-alive connections from Fly's proxy would trigger it.
2. **A count of requests being handled, checked first**, with a cheap refusal past a cap.

**Which status to shed with**
1. **503**, as brief §11.5 says for a slow database. The SDK retries a 5xx 3 times (after about
   1, 2 and 4 s) and then drops the batch. A shed lasting more than a few seconds would lose
   data.
2. **429 with `Retry-After`.** The SDK keeps the batch and waits, as for a rate limit.

**Junk**
1. Check that requests come from the real SDK, with a handshake or signing. Impossible: the SDK
   is open source and runs on the user's machine, so any secret in it can be copied. A key
   proves the account, never the software.
2. **Lower the inflate cap, and charge rejected bodies by size.**

## Decision
**Shedding.** Ingest counts requests between the shed check and their answer
(`app.state.in_flight`). The check comes after the header checks and before the key lookup, so
cold key lookups are bounded too. At `MAX_IN_FLIGHT` (50), a new request gets
`429 {"error": "overloaded"}` at once. Its `Retry-After` is a random 3-10 s, so shed SDKs don't
all come back together. A slow or unreachable database still answers 503, as before.

**Junk.**
- `MAX_DECOMPRESSED` is now 2 MB, down from 5 MB.
- A body rejected after the key is known costs the account one rate-limit token per 1,000
  bytes (`REJECTED_BYTES_PER_TOKEN`), about a record's worth:
  - read but over `MAX_BODY`: the bytes read;
  - over the inflate cap, or corrupt gzip: the cap, since how much was inflated before the
    fault is unknown;
  - malformed JSON, an invalid envelope, or too many records: the inflated size.
- A zip bomb now costs 2,000 tokens. That is most of a free account's 3,000 burst, so the
  account is then refused before its next body is even read.

Measured after the change (local stack, `docs/capacity.md`):
- At 300-400 req/s, ingest stores 290-340 records/s, where it stored about 10. There are no
  timeouts, and the extra requests get a 429 in about 5 ms.
- A junk flood from 10 accounts is refused after about 40 bodies. App CPU fell from 0.87 to
  0.33 of a core, and real traffic's p95 from 140-850 ms to 24 ms.

## Trade-offs
- **Accepted requests can wait up to about 0.5 s (p95) while shedding.** That is 50 waiting at
  the database's pace. A lower cap would cut this, but would shed during short blips. A shed
  costs the SDK only a few seconds' delay.
- **Shedding is per machine and counts requests, not CPU.** A flood of cheap requests (bad keys)
  can still use the whole core. That is for Cloudflare's per-IP limit, and the app's per-IP
  limit on invalid keys, at step 8.
- **The 429 `overloaded` code is a new reason.** It isn't a rate limit on the account, and
  Grafana should show it apart from `rate_limited` (it does: the `reason` label).
- **An account sending junk locks itself out for minutes.** Only a client other than the SDK
  can do that.
