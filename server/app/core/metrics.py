"""Prometheus metrics about our own service (brief §9.10, ADR 0021).

Served on their own port (`METRICS_PORT`), which Fly scrapes over its private network; the
public port never serves them. Reading them never touches the database, so scrapes cannot wake
it. Labels take only fixed values (status codes, reason codes, record types): never project,
account or key IDs, model names or anything else a client sends.
"""

from prometheus_client import Counter, Gauge, Histogram, disable_created_metrics

# A `_created` series per counter doubles the scrape for no use here.
disable_created_metrics()  # type: ignore[no-untyped-call]

# Seconds. Typical batches take ~1 ms; the load test's target is p95 under 200 ms (§20.6).
_LATENCY = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.5, 1.0, 2.5)

REQUESTS = Counter(
    "gaslit_ingest_requests",
    "Ingest requests answered, by HTTP status and reason code.",
    ["status", "reason"],
)
REQUEST_SECONDS = Histogram(
    "gaslit_ingest_request_seconds", "Time to answer an ingest request.", buckets=_LATENCY
)
RECORDS = Counter(
    "gaslit_ingest_records",
    "Records in batches answered 202, by type and outcome, as in the response.",
    ["type", "outcome"],
)
BATCH_RECORDS = Histogram(
    "gaslit_ingest_batch_records",
    "Records (summaries plus samples) per batch answered 202.",
    buckets=(1, 2, 5, 10, 25, 50, 100, 200, 500, 1000),
)
INSERT_SECONDS = Histogram(
    "gaslit_db_insert_seconds", "Time to insert one batch, failures included.", buckets=_LATENCY
)
KEY_LOOKUPS = Counter(
    "gaslit_key_lookups",
    "Key lookups that reached the database, each one a write, by whether the key was found.",
    ["result"],
)
QUOTA_FLUSHES = Counter(
    "gaslit_quota_flushes", "Usage flushes to monthly_usage, by result.", ["result"]
)
DASHBOARD_QUERY_SECONDS = Histogram(
    "gaslit_dashboard_query_seconds",
    "Time to run one dashboard read, by query, failures included. Its p95 over 300 ms is when "
    "pre-summed data is needed (ADR 0008).",
    ["query"],
    buckets=_LATENCY,
)
PARTITIONS_MAINTAINED = Gauge(
    "gaslit_partitions_last_success_timestamp_seconds",
    "When the worker last finished its upkeep: partitions, totals and retention (ADR 0026). "
    "Partitions run out 7 days after it.",
)
