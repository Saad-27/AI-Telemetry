# 0024: Load test with our own generator, on Docker limits that copy production

- **Status:** accepted
- **Date:** 2026-10-18

## Context
Brief §20.6 and M3 step 7 ask for a load test of `POST /v1/ingest`:
- **Scenarios:** steady load, a 10× spike, a slow or unavailable database, an invalid-key
  flood, and oversized or malformed payloads.
- **Measures:** maximum requests/s and records/s with p95 under 200 ms, database CPU and I/O,
  rollup lag and error rates.
- **Results** go in `docs/capacity.md`, with machine sizes.

Brief §9.10 suggests k6 or Locust and asks for the number "on the smallest production setup".
Production is not hosted yet (step 8, ADR 0007), so the test must run locally now and again on
Fly and Neon once they exist.

Checked on 2026-10-18 ([Fly CPU performance](https://docs.fly.io/machines/cpu-performance)): a
`shared-cpu-1x` machine is guaranteed 5 ms of CPU per 80 ms (6.25%). Unused time builds a burst
balance: it starts at 5 s and is capped at 500 s. While the balance lasts, the machine can use
the whole core. The docs don't give the accrual rate.

## Options considered
**Generator**
1. **k6** (a Go binary, scripts in JavaScript). It would need the SDK's record shapes rewritten
   in JavaScript, and a binary installed on every machine that runs the test.
2. **Locust** (Python). It is a new dependency with a web UI we don't need. Its default user
   model is closed-loop: a user waits for each answer before sending the next request. A slow
   server then gets fewer requests, which hides its latency.
3. **Our own script** (`tools/loadtest/loadtest.py`, about 500 lines, no new dependency). It
   builds bodies with `server/tests/records.py`, the same builders the tests use. It runs open
   loop: each request leaves at its scheduled time and its latency counts from that time.

**Where to run it before hosting**
1. Wait for step 8. Then the design gets no feedback before it is paid for.
2. **Locally, in containers limited like production.** The app runs as the production image in
   256 MB, with either a whole core (a `shared-cpu-1x` machine with burst balance) or 5 ms per
   80 ms (a machine whose balance is spent). Postgres 17 gets 0.25 CPU and 1 GB, like Neon at
   0.25 CU.

## Decision
Use our own generator, run locally against `tools/loadtest/run.sh` containers. Rerun the same
generator against Fly and Neon in step 8, and before each stage change (brief §20.6).
- A `setup` command makes 1,000 accounts with keys. It also adds a week of history for a busy
  account (12 summaries a minute) and a typical one (2 a minute), so dashboard reads can be
  timed under load.
- Each `run` sends one stream of traffic at stepped rates, plus optional floods at fixed rates.
  It prints:
  - status codes and p50/p95/p99 latency, per interval and per step;
  - each container's CPU (from its cgroup) and peak memory;
  - the database's WAL bytes, as a measure of I/O;
  - optionally, the dashboard repository's read times.
- Up to 64 keep-alive connections per process, standing in for Fly's proxy. A request that
  waits more than 5 s, the SDK's timeout, counts as an error.

## Trade-offs
- **Local results understate network costs.** Each request makes 2 or 3 database round trips.
  On Neon each one crosses the network, and each commit waits for its WAL to be stored across
  availability zones. Expect higher latency at the same CPU cost.
- **Docker has no burst balance.** "Baseline" mode is the worst case: a machine whose balance
  is already spent.
- **Neon's 0.25 CU is modelled as a CPU quota.** This is an assumption to check in step 8.
- Generator and server share one host (AMD Ryzen 7 2700X, 16 threads, WSL2). Cgroup limits
  keep them apart, and the generator used under half a core.
- We maintain the script ourselves. It stays small because it reuses the test builders. If
  tests need distributed load, k6 is the fallback.
