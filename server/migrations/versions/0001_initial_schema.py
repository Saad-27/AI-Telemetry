"""Tenant tables, day-partitioned data tables, least-privilege roles and row-level security.

See ADR 0013. Partitions are created by app.db.partitions, not here.

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Error classes from brief §6.5, one count column each (cancelled has its own column).
ERROR_CLASSES = (
    "rate_limited",
    "quota_error",
    "auth_error",
    "client_error",
    "overloaded",
    "server_error",
    "timeout",
    "connection_error",
    "unknown",
)

_ERROR_COLUMNS = ",\n".join(f"err_{c} integer NOT NULL DEFAULT 0" for c in ERROR_CLASSES)

# NULLIF: once a SET LOCAL ends, the setting reads as '' (not NULL) for the rest of the session.
_CURRENT_PROJECT = "NULLIF(current_setting('app.project_id', true), '')::uuid"

UPGRADE = [
    # Accounts. Sessions and OAuth identities arrive with sign-in (M4).
    """
    CREATE TABLE users (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        email text NOT NULL,
        display_name text,
        plan text NOT NULL DEFAULT 'free',
        created_at timestamptz NOT NULL DEFAULT now(),
        deleted_at timestamptz
    )
    """,
    """
    CREATE TABLE projects (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        name text NOT NULL,
        owner_user_id uuid NOT NULL REFERENCES users (id),
        created_at timestamptz NOT NULL DEFAULT now(),
        deleted_at timestamptz
    )
    """,
    "CREATE INDEX projects_owner_user_id_idx ON projects (owner_user_id)",
    """
    CREATE TABLE project_members (
        project_id uuid NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
        user_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        role text NOT NULL DEFAULT 'owner',
        PRIMARY KEY (project_id, user_id)
    )
    """,
    "CREATE INDEX project_members_user_id_idx ON project_members (user_id)",
    # Only the SHA-256 of a key is stored (brief §11.4).
    """
    CREATE TABLE ingest_keys (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        project_id uuid NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
        key_hash bytea NOT NULL UNIQUE CHECK (length(key_hash) = 32),
        display_prefix text NOT NULL,
        last4 text NOT NULL,
        created_by uuid REFERENCES users (id) ON DELETE SET NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        last_used_at timestamptz,
        revoked_at timestamptz
    )
    """,
    "CREATE INDEX ingest_keys_project_id_idx ON ingest_keys (project_id)",
    # Append-only data (brief §19.4), one partition per UTC day, dropped at retention. No
    # foreign key to projects: ingest already resolved the project from the key, and project
    # deletion removes these rows with a job rather than a cascade across partitions.
    f"""
    CREATE TABLE summary_records (
        project_id uuid NOT NULL,
        minute timestamptz NOT NULL,
        summary_id uuid NOT NULL,
        received_at timestamptz NOT NULL DEFAULT now(),
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
        count integer NOT NULL,
        ok integer NOT NULL,
        cancelled integer NOT NULL,
        {_ERROR_COLUMNS},
        ttft_hist integer[] NOT NULL,
        duration_hist integer[] NOT NULL,
        decode_hist integer[] NOT NULL,
        input_tokens_sum bigint NOT NULL,
        output_tokens_sum bigint NOT NULL,
        output_tokens_estimated_count integer NOT NULL,
        PRIMARY KEY (project_id, minute, summary_id)
    ) PARTITION BY RANGE (minute)
    """,
    # The primary key also serves the live feed: a B-tree reads backwards just as fast, so no
    # separate (project_id, ts DESC) index is needed.
    """
    CREATE TABLE call_samples (
        project_id uuid NOT NULL,
        ts timestamptz NOT NULL,
        event_id uuid NOT NULL,
        received_at timestamptz NOT NULL DEFAULT now(),
        reason text NOT NULL,
        duration_ms real NOT NULL,
        ttft_ms real,
        provider text NOT NULL,
        route text NOT NULL,
        endpoint_host text,
        endpoint_region text,
        operation text NOT NULL,
        model_requested text NOT NULL,
        model_returned text,
        streaming boolean NOT NULL,
        has_tools boolean NOT NULL,
        input_tokens integer,
        output_tokens integer,
        cached_input_tokens integer,
        output_tokens_estimated boolean NOT NULL,
        input_bucket text NOT NULL,
        status text NOT NULL,
        error_class text,
        http_status smallint,
        provider_error_code text,
        service text,
        environment text,
        label text,
        origin_region text,
        provider_sdk text,
        provider_sdk_version text,
        PRIMARY KEY (project_id, ts, event_id)
    ) PARTITION BY RANGE (ts)
    """,
    # Group roles (brief §11.6). NOLOGIN: login roles are created outside migrations and
    # granted membership, so no password lives here. Roles are shared by every database on
    # the server, hence IF NOT EXISTS.
    """
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ingest_writer') THEN
            CREATE ROLE ingest_writer NOLOGIN;
        END IF;
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'dashboard_app') THEN
            CREATE ROLE dashboard_app NOLOGIN;
        END IF;
    END
    $$
    """,
    "GRANT USAGE ON SCHEMA public TO ingest_writer, dashboard_app",
    # Ingest looks keys up by hash and appends records. It cannot read anything it wrote.
    "GRANT SELECT (id, project_id, key_hash, revoked_at) ON ingest_keys TO ingest_writer",
    "GRANT UPDATE (last_used_at) ON ingest_keys TO ingest_writer",
    "GRANT INSERT ON summary_records, call_samples TO ingest_writer",
    # The dashboard's grants grow with its routes in M4.
    "GRANT SELECT ON summary_records, call_samples TO dashboard_app",
]

# Row-level security (D10): a dashboard query sees only the project set with
# `SET LOCAL app.project_id`, so a missed WHERE clause cannot leak another tenant's rows.
for _table in ("summary_records", "call_samples"):
    UPGRADE += [
        f"ALTER TABLE {_table} ENABLE ROW LEVEL SECURITY",
        f"CREATE POLICY ingest_insert ON {_table} FOR INSERT TO ingest_writer WITH CHECK (true)",
        f"CREATE POLICY tenant_read ON {_table} FOR SELECT TO dashboard_app "
        f"USING (project_id = {_CURRENT_PROJECT})",
    ]

# Dropping a table drops its grants and policies. The roles stay: other databases on the same
# server may still use them.
DOWNGRADE = [
    "DROP TABLE call_samples",
    "DROP TABLE summary_records",
    "DROP TABLE ingest_keys",
    "DROP TABLE project_members",
    "DROP TABLE projects",
    "DROP TABLE users",
    "REVOKE USAGE ON SCHEMA public FROM ingest_writer, dashboard_app",
]


def upgrade() -> None:
    # One statement per call: asyncpg prepares each one, which allows a single statement only.
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
