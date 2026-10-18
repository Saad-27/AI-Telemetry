"""5-minute and hourly totals for the dashboard, where they are built to, and late marks.

See ADR 0023. Partitions are made by app.db.partitions, like the summaries'.

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

ERROR_CLASSES = (
    "rate_limited", "quota_error", "auth_error", "client_error", "overloaded", "server_error",
    "timeout", "connection_error", "unknown",
)  # fmt: skip

_CURRENT_PROJECT = "NULLIF(current_setting('app.project_id', true), '')::uuid"


def _totals(table: str) -> str:
    """One row per project, bucket and series. bigint throughout: a bucket sums many summaries,
    each allowed up to 10 million calls, and an overflow would stop every account's build."""
    errors = ",\n".join(f"err_{c} bigint NOT NULL" for c in ERROR_CLASSES)
    return f"""
    CREATE TABLE {table} (
        project_id uuid NOT NULL,
        bucket timestamptz NOT NULL,
        provider text NOT NULL,
        route text NOT NULL,
        endpoint_host text,
        endpoint_region text,
        operation text NOT NULL,
        model text NOT NULL,
        streaming boolean NOT NULL,
        input_bucket text NOT NULL,
        service text,
        environment text,
        origin_region text,
        count bigint NOT NULL,
        ok bigint NOT NULL,
        cancelled bigint NOT NULL,
        {errors},
        ttft_hist bigint[] NOT NULL,
        duration_hist bigint[] NOT NULL,
        decode_hist bigint[] NOT NULL,
        input_tokens_sum bigint NOT NULL,
        output_tokens_sum bigint NOT NULL,
        output_tokens_estimated_count bigint NOT NULL
    ) PARTITION BY RANGE (bucket)
    """


UPGRADE = [
    # Rebuilt by deleting a bucket's rows and inserting them again, so no unique key is needed.
    _totals("totals_5m"),
    "CREATE INDEX totals_5m_project_bucket_idx ON totals_5m (project_id, bucket)",
    _totals("totals_1h"),
    "CREATE INDEX totals_1h_project_bucket_idx ON totals_1h (project_id, bucket)",
    # One row: totals exist for the 5-minute buckets before this. Starts where the summaries
    # start, so the first build covers them.
    """
    CREATE TABLE totals_watermark (
        one boolean PRIMARY KEY DEFAULT true CHECK (one),
        built_through timestamptz NOT NULL
    )
    """,
    """
    INSERT INTO totals_watermark (built_through)
    SELECT date_bin('5 minutes', coalesce(min(minute), now()), '2000-01-01T00:00:00+00')
    FROM summary_records
    """,
    # Buckets that received summaries after they may have been built.
    """
    CREATE TABLE totals_stale (
        project_id uuid NOT NULL,
        bucket timestamptz NOT NULL,
        PRIMARY KEY (project_id, bucket)
    )
    """,
    "GRANT INSERT ON totals_stale TO ingest_writer",
    "GRANT SELECT ON totals_5m, totals_1h, totals_watermark TO dashboard_app",
]
# The same row-level security as the summaries (ADR 0013).
for _table in ("totals_5m", "totals_1h"):
    UPGRADE += [
        f"ALTER TABLE {_table} ENABLE ROW LEVEL SECURITY",
        f"CREATE POLICY tenant_read ON {_table} FOR SELECT TO dashboard_app "
        f"USING (project_id = {_CURRENT_PROJECT})",
    ]

DOWNGRADE = [
    "DROP TABLE totals_stale",
    "DROP TABLE totals_watermark",
    "DROP TABLE totals_1h",
    "DROP TABLE totals_5m",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
