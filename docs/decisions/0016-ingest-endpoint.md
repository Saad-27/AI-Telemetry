# 0016: The ingest endpoint: checks, responses and writes

- **Status:** accepted; gunzip cap (now 2 MB), charges for rejected bodies and load shedding amended by [0025](0025-load-shedding-and-rejected-body-charges.md)
- **Date:** 2026-10-17

## Context
Brief §9.2 and §11.5 list the steps of `POST /v1/ingest`, the limits and the responses, and
§6.4 the validation rules. They leave open the order of the cheap checks, the codes for cases
they don't list, what an error entry may say, how far validation goes beyond §6.4, and how a
batch is written. Rate limits and quotas are step 4 (§9.2 step 3 is not built yet).

Checked on 2026-10-17: Pydantic 2.13 in strict mode accepts an int for a float, and rejects
`"1"` for an int and `1` for a bool. Its error locations include unknown keys and dict keys as
the client sent them. `pydantic_core.from_json` (installed with FastAPI) rejects `NaN`,
numbers over 4,300 digits and nesting deeper than 200 levels in under 1 ms.

## Options considered
**Records with errors in a valid batch**
1. Reject the batch with `400`: simple, but the SDK drops the whole batch for one bad record.
2. Validate each record on its own, store the good ones (brief §6.4).

**Writing summaries and samples**
1. One transaction for both inserts: atomic, but two more round trips.
2. Two plain statements: if the second fails, the SDK's retry re-sends both, and the idempotent
   IDs keep only one copy of each.

## Decision
- **Order:** `Content-Length` over 1 MB → 413; `Content-Type` not JSON or `Content-Encoding`
  not gzip/identity → 415; key (format and checksum, then the cached lookup) → 401; body read
  with a 1 MB cap (chunked uploads included) → 413; gunzip with a 5 MB cap → 413/400; JSON →
  400; envelope → 400; over 1,000 records → 413, which makes the SDK split the batch; then
  each record. Any database error, in the key lookup or the insert → `503` with
  `Retry-After: 5`, never 401, which would stop the SDK.
- **Option 2 for records.** `202 {"accepted", "rejected", "errors"}`; each error is
  `{"array": "samples", "index": 3, "reason": "ttft_ms: less_than_equal"}`, at most 20. The
  reason is the field path, kept only while each part is one of our field names, plus
  Pydantic's error type. It never holds input, an unknown key or a dict key, which is where
  content could hide.
- **Validation beyond §6.4:**
  - JSON types are strict; every field must be present (nullable ones may be `null`).
  - `endpoint_host` must be on the SDK's allowlist (ADR 0009). Azure arrives as the suffix only.
  - `endpoint_region` matches `^[a-z0-9-]{1,32}$`; `provider_sdk_version`, SDK and runtime
    versions match `^[A-Za-z0-9._+-]{1,32}$`.
  - Summaries must add up: `count = ok + cancelled + Σ errors`, `Σ duration_hist = ok`,
    `Σ ttft_hist ≤ count`, `Σ decode_hist ≤ ok`, token sums ≤ `count × 10M`. `minute` is a
    whole minute. `count` is 1 to 10M (the columns are 32-bit).
  - `ttft_ms` may exceed `duration_ms` by 1 ms (the SDK rounds them separately).
  - `ts` and `minute` within [now − 24 h, now + 5 min].
- **Writes, option 2.** One `INSERT ... SELECT FROM unnest() ON CONFLICT DO NOTHING` per
  record type (ADR 0013), behind the `Sink` protocol (`PostgresSink`). Histograms travel as
  text and are cast back per row, because `unnest` flattens arrays of arrays. `received_at` is
  the database default. Samples get `input_bucket` from `input_tokens` (the SDK's rule).
- **Logging:** one INFO line per request: status, code or project and counts, and time. The
  app turns on INFO with `logging.basicConfig`; Uvicorn only configures its own loggers.
- **Local keys:** `make dev-key` creates a dev user and project once and a new key each run.
  It refuses to run on Fly.

## Trade-offs
- Parsing and validation run on the event loop: ~0.5 ms for a typical SDK batch, 19-35 ms for
  1,000 records (measured). The load test (step 7) decides whether to move it to a thread.
- `accepted` counts duplicates that `ON CONFLICT` skipped; a replay looks the same as the first
  send, which is what the SDK needs.
- Records more than 24 h old are rejected, so an SDK that queued through a long outage loses them.
- The enums and host list are copies of the SDK's. Running the examples against the server (all
  accepted on 2026-10-17) catches drift; step 6 makes it a test.
- A record for a day without a partition fails the batch with 503. `make migrate` makes a
  week of partitions; how production makes them is step 5.
