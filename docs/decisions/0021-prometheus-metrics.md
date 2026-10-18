# 0021: Prometheus metrics on a private port, scraped by Fly

- **Status:** accepted
- **Date:** 2026-10-18

## Context
Brief §9.10 asks for Prometheus metrics about our own service:
- ingest requests by status;
- records accepted and rejected;
- batch size;
- ingest and insert latency;
- quota rejections.

ADRs 0019 and 0020 add two needs:
- seeing how often the database is really reached;
- alerting long before the partitions made a week ahead run out.

Constraints:
- The metrics must never wake the database (ADR 0007).
- They must not reveal tenants. Labels with IDs would leak them and grow without bound.
- They must not be public: `gaslit-api-production.fly.dev` reaches the app without Cloudflare
  (`security-config.md`).

Checked on 2026-10-18 ([Fly metrics](https://docs.fly.io/monitoring/metrics)):
- A `[metrics]` section in `fly.toml` makes Fly scrape a port every 15 s.
- Fly stores the results in its managed Prometheus, with Grafana at fly-metrics.net.
- Both are free for now.
- The app should listen on `0.0.0.0`.
- Fly's docs do not say whether scrapes keep a machine running or start a stopped one.

## Options considered
**Library**
1. **`prometheus_client`** (0.26.0, no required dependencies): the reference client, with
   correct text format and the process collector (memory, CPU, open files) included.
2. **Writing the text format by hand.** About as much code as the library's part we use, and
   easy to get subtly wrong (escaping, histogram buckets).

**Where to serve it**
1. `/metrics` on the public app port. It would be reachable from the internet unless it is
   filtered.
2. **A separate port that is not in `[http_service]`,** so Fly's proxy never routes to it.

## Decision
`prometheus_client`, served by its own HTTP server thread on `METRICS_PORT` (9091 on Fly and in
compose, off when unset). It runs in a thread, so a scrape never waits for the event loop.
Fly scrapes it. The public port answers `/metrics` with 404.

| Metric | Type | Labels |
|---|---|---|
| `gaslit_ingest_requests_total` | counter | `status`, `reason` (the response's code: `accepted`, `quota_exceeded`, `rate_limited`, `invalid_key`, ...) |
| `gaslit_ingest_request_seconds` | histogram, 1 ms to 2.5 s | none |
| `gaslit_ingest_records_total` | counter, batches answered 202 only, like the response | `type` (`summary`, `sample`), `outcome` (`accepted`, `rejected`) |
| `gaslit_ingest_batch_records` | histogram, batches answered 202 | none |
| `gaslit_db_insert_seconds` | histogram, failures included | none |
| `gaslit_key_lookups_total` | counter: lookups that reached the database (each one a write) | `result` (`found`, `missing`) |
| `gaslit_quota_flushes_total` | counter | `result` (`ok`, `failed`) |
| `gaslit_partitions_last_success_timestamp_seconds` | gauge | none |
| `gaslit_dashboard_query_seconds` (added by [0022](0022-read-time-sums.md)) | histogram, 1 ms to 2.5 s, failures included | `query` (`overview`, `series`) |

- **Labels take fixed values only:** never project, account or key IDs, model names or
  anything a client sends. A test checks the scraped text for the IDs, the key and the model.
- `_created` series are turned off; a scrape is about 5.5 KB.
- **Alerts to set up** with Grafana at hosting:
  - no successful partition run for 2 days (they run out after 7);
  - 503s;
  - quota flush failures;
  - p95 latency over 200 ms (§20.6).
- Dashboard query latency (§9.10, until rollups exist) comes with the queries in step 6.

## Trade-offs
- One more dependency in the server, justified by §9.10. No new service: Fly's Prometheus and
  Grafana are part of the platform.
- Counters live in each process and restart at zero. Prometheus `rate()` handles that, but a
  machine stopped between scrapes loses the counts since the last scrape.
- If scrapes kept a stopped machine from stopping, they would cost the machine's time (about
  $2 a month) but never wake the database. Check this once hosted, with ADR 0019's open point.
- The metrics add about 7 µs per accepted request (measured), against about 0.5 ms for a
  typical batch.
- Stopping the metrics thread takes up to 0.5 s at shutdown, inside Fly's 5 s stop timeout.
