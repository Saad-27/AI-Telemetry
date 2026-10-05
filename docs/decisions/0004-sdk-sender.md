# 0004: SDK sender, retries and lifecycle

- **Status:** accepted
- **Date:** 2026-10-05

## Context
Brief §6.7 and §19.3 set the rules: one background thread, no I/O on the caller's thread,
stdlib HTTP, a bounded queue, a fixed response table, and flush on exit and after fork. Some
details are left open, and the obvious implementations have traps (unbounded memory during
an outage, keys leaking on redirect, a hung ingest blocking exit).

## Options considered
1. **Retries sleep inside the send loop.** Simple, but while the thread sleeps nobody drains
   the aggregator, so closed minutes pile up without bound during a long 429 or outage.
2. **A non-blocking state machine.** The thread wakes on a timer, on a full batch or on
   `flush()`. Each cycle moves closed minutes into the bounded queue, then sends only if no
   backoff is in force. Memory stays bounded by `max_queue` whatever the ingest does.
3. **`http.client` with a persistent connection** instead of `urllib.request`. Saves a TLS
   handshake per batch, but proxy support (`HTTPS_PROXY`, `NO_PROXY`) would have to be
   rewritten by hand.

## Decision
Option 2, using `urllib.request`.

**Thread:** one daemon thread, started on the first recorded call. It wakes at
`min(flush_interval, next minute close)` or when the queue reaches `max_batch`. A minute
closes 2 s after it ends. Summaries share the bounded queue with samples, so an outage drops
and counts records rather than growing memory.

**Batches:** up to `max_batch` records each. Record IDs are made once, when the batch is
formed, so a retried batch has the same `summary_id`/`event_id` values and the server can
de-duplicate it. `dropped_since_last` counts every record not accepted (queue overflow,
retries exhausted, 400, unsplittable 413) and resets on a 2xx.

**Responses** (brief §6.7 table):

| Response | Behaviour |
|---|---|
| 2xx | Done |
| 401 / 403 | Warn once, stop the thread and turn the SDK off for this process |
| 429 | Wait for `Retry-After` (delta-seconds only, capped at 300 s; 30 s if missing or invalid), then retry the same batch, with no limit on attempts |
| 413 | Split the batch in half once; a half that gets 413 again is dropped |
| 5xx or network error | Retry after ~1, 2 and 4 s (×0.5-1.5 jitter), then drop |
| 400, 3xx, other 4xx | Drop, warn once, move on to the next batch |

**HTTP hardening**
- Redirects are not followed. urllib would forward the `Authorization` header to the new
  URL. This also covers an endpoint that gets compromised or mistyped.
- TLS uses an explicit `ssl.create_default_context()`, so verification stays on even if the
  app has monkeypatched `ssl._create_default_https_context`.
- The `http://` loopback endpoint used in local dev bypasses proxies.
- A 5 s socket timeout applies to connect and to each read.

**`flush(timeout)` and `shutdown(timeout)`:** the caller closes the current partial minute,
wakes the thread and **waits** up to `timeout`. The thread does the I/O, so the
no-I/O-on-the-caller rule holds and the timeout is a hard cap even if the ingest hangs.
`shutdown()` also turns the SDK off, and `init()` registers it with `atexit` (2 s cap).

**Fork:** `os.register_at_fork(after_in_child=...)` gives the child a fresh state (empty
aggregator and queue, no thread) and a new module lock. The parent still sends what it
measured before the fork, so nothing is counted twice.

**Debug mode:** prints the exact JSON body (before gzip) to stderr for every attempt. Without
a key it prints and sends nothing.

**Fake ingest:** `sdk/python/tests/fake_ingest.py`, using only the stdlib. It applies the
§9.2 size limits, rejects unknown fields like the server's strict schema, and replies as
scripted. It also runs standalone for local development.

**Benchmark:** `tests/bench_overhead.py` runs in CI on every Python version, sending to the
fake ingest, and fails if the median goes over the §6.8 budget. First run (WSL2, local):
7-12 µs per `track()` call and 0.06-0.11 µs per streamed chunk, against budgets of 50 µs
and 2 µs.

## Trade-offs
- A new TLS connection per batch: at most one every `flush_interval`, so cheap. Revisit
  with `http.client` keep-alive only if the benchmark or load tests show it matters.
- The 5 s timeout is per socket operation, not a total, so a server that drips bytes can
  hold the thread longer. That only delays sending: the app's call path never waits, and
  `flush`/`shutdown` are capped on the waiting side.
- Retry-After in HTTP-date form falls back to 30 s. Our own server sends seconds.
- After a 429, records keep queuing and the oldest stay at the front. If the outage outlasts
  the queue, new records are dropped rather than old ones.
- The benchmark threshold is the budget itself, not a tighter regression baseline. CI runner
  noise makes tighter thresholds flaky. Tighten it once there is a history of numbers.
