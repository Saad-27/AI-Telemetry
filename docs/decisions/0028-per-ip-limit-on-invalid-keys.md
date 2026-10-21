# 0028: Per-IP limit on invalid keys

- **Status:** proposed (implements brief §11.5; the numbers are for Saad to confirm)
- **Date:** 2026-10-21

## Context
Brief §11.5 asks for a per-IP rate limit on invalid-key attempts. Made-up keys with a valid
checksum each cost an indexed database lookup, since only keys found are cached (ADR 0015).
In the step 7 load test, 200 such keys a second used 0.17 of the database's 0.25 CPU
(`docs/capacity.md`). Cloudflare's per-IP rule (300 per 10 s) still lets one IP send 30 a
second.

The app needs a client IP it can trust. Behind Fly's proxy the socket address is Fly's.
Cloudflare sends the client's address in `CF-Connecting-IP` and overwrites any value the
client sent, but a request straight to `*.fly.dev` could carry any value. Since ADR 0027, every
request that passes the edge check came through Cloudflare.

## Decision
- **Client IP.** `EdgeOnly` (`app/core/edge.py`) makes `CF-Connecting-IP` the request's client
  address (`scope["client"]`), but only on requests that carried the right edge secret. Without
  edge secrets (local, tests) the socket address is used and the header is ignored.
- **The limit** (`InvalidKeys`, `app/ingest/limits.py`): a token bucket per IP, 20 attempts,
  refilling at one every 2 s. IPv6 addresses count per /64, the block one customer usually
  gets. Only IPs that sent an invalid key are stored (at most 10,000, least recent dropped).
- **Where it applies.** Ingest first checks the key cache (`KeyStore.cached`). A cached key is
  served with no IP check, so the common path costs nothing extra and others behind the same
  address (an office, a cloud NAT) keep sending while a neighbour floods. Only a key that would
  need the database checks the IP: while its bucket is empty, the answer is `429
  too_many_invalid_keys` with `Retry-After` (seconds until one token, at least 1), with no
  lookup. Every 401 (missing, malformed, unknown or revoked key) takes a token.
- **429, not 401.** After a 401 the SDK stops sending for good, so a valid key that is not
  cached yet, sent from a busy IP, would be silenced. After a 429 the SDK keeps its batch and
  retries after `Retry-After`. A fleet with a revoked key still hears 401, at most one every
  2 s per IP once the burst is used.
- **Quieter disconnects (capacity.md proposal 7).** A client that hangs up mid-body is answered
  `499 disconnected` (counted in the metrics, one log line) instead of a traceback.

## Measured (local stack, ADR 0024, app on 1 core)
200 unknown keys a second from one IP for 20 s: 76 reached the database (was 4,000), then
about one every 2 s. Database CPU 0.02 (was 0.17), app 0.25 of a core, p95 4.3 ms.

## Trade-offs
- Requests in flight all pass the check before the first failures are counted, so the first
  burst can exceed 20: 70 got through in the first second of the test above. It is bounded by
  the 50 requests in flight (ADR 0025); after that the rate holds.
- An uncached valid key sent from an IP that is flooding waits for a token. It is a rare case
  (a NAT shared with an attacker) and the SDK retries.
- Buckets are per machine, like the account rate limit (ADR 0017).
- An attacker with many addresses (an IPv6 /48 has 65,536 /64s) can push buckets out and get
  around the limit. Cloudflare's rule and the account limits still apply; revisit if Grafana
  shows key lookups from many addresses.
- Cloudflare Worker requests from other zones arrive with a fixed Worker address, so they all
  share one bucket. No real SDK traffic comes that way.
