# 0003: SDK wire semantics (summaries, histograms, sampling)

- **Status:** accepted
- **Date:** 2026-10-02

## Context
Brief §19 fixes the wire format (D8) but leaves some semantics open. Old SDK versions stay
installed for years, so these rules must be fixed before the first release. Items marked
**(Saad)** were decided with him on 2026-10-02. The rest are defaults he did not object to.

## Decisions

**Histograms**
- Bounds are those in brief §9.4: 20 latency buckets and 14 decode buckets, each ending in +inf.
- Buckets are **upper-inclusive** (value ≤ bound), as in Prometheus. Index = `bisect_left(bounds, v)`.
- Which calls each histogram counts:
  - `duration_hist`: **successful calls only** **(Saad)**. This differs from the §19.2 example,
    which counted every call. Otherwise fast 401s or 500s pull p50 duration down and can hide a
    slowdown on the health badge, and errors are already visible in the error counts.
  - `ttft_hist`: any call that reached a first output chunk, whatever happened afterwards.
  - `decode_hist`: successful streaming calls with ≥ 16 non-estimated output tokens and
    `duration > ttft`.

**Summaries**
- A call is counted in the minute it **finishes** (`ts + duration`).
- The series key is `(provider, route, endpoint_host, endpoint_region, operation, model,
  streaming, input_bucket)`. `model` is the returned model, falling back to the requested one.
  `service`, `environment` and `origin_region` are fixed per process and added when the record
  is serialised.
- `label` stays on samples only, not in the series **(Saad)**. Adding it later as an optional
  series field would not break older SDKs.
- A process may send **more than one summary for the same minute and series**, for example after
  `flush()` closes a partial minute. That's safe because the server sums summaries.
- Cardinality guard: at most 500 series per process per minute. Any further series is folded
  into the same key with `model="other"`, and the folded calls are counted.

**Error classes** (brief §6.5, from status and short code only)
- `insufficient_quota` → `quota_error`. `overloaded_error` or 529 → `overloaded`.
- 401/403 → `auth_error`, 408 → `timeout`, 429 → `rate_limited`.
- Any other 4xx → `client_error`, any other 5xx → `server_error`, anything else → `unknown`.
- Provider-specific codes are checked against current docs in M2 (§17).

**Samples** (checked in this order, under the aggregator's lock)
1. Errors: always, up to 100 per process per minute. Errors over the cap are only counted.
2. First call of each series in the process lifetime. The set of seen series is capped at 500;
   once it's full, no more `first_of_series` samples are sent. An erroring first call is sent as
   `error` and still marks the series as seen.
3. Random: successful calls only, at `sample_rate`, up to `max_samples_per_minute`.

**Manual calls (`track()`)** **(Saad)**
- An optional `route=` argument, defaulting to `custom` (or `local` when `provider="local"`).
  `endpoint_host` is always null, because `track()` never sees a host.
- `t.set_error(http_status=None, code=None)` records an error without raising, using the same
  classification as above.
- `provider_sdk` and `provider_sdk_version` are null, because no provider SDK is involved.

**Encoding:** `duration_ms` and `ttft_ms` are rounded to 0.1 ms.

## Options considered
For `duration_hist`: count every call (as in the §19.2 example) or successful calls only. Counting
every call keeps one number for "what users waited", but it mixes the error mix into latency.

## Trade-offs
- p50/p95 duration no longer reflects how long failing calls took. A timeout storm shows up as
  errors rather than as latency.
- Upper-inclusive buckets mean a value exactly on a bound sits in the lower bucket. The server's
  interpolation must use the same rule.
- Revisit any of this only together with a new `schema_version`.
