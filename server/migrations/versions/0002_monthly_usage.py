"""Calls measured and records stored per account per month, and what ingest reads to apply
limits.

See ADR 0017. Ingest adds its in-memory counts here every few seconds; the dashboard's banner
reads it in M4.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

UPGRADE = [
    # One row per account per UTC month. Not project-scoped, so no RLS: the dashboard reads it
    # by the signed-in user's id (M4).
    """
    CREATE TABLE monthly_usage (
        user_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        month date NOT NULL CHECK (extract(day FROM month) = 1),
        calls bigint NOT NULL CHECK (calls >= 0),
        records bigint NOT NULL CHECK (records >= 0),
        PRIMARY KEY (user_id, month)
    )
    """,
    # The key lookup joins to the project's owner for the plan, and skips deleted projects.
    # Columns only: ingest still cannot read names, emails or anything else.
    "GRANT SELECT (id, owner_user_id, deleted_at) ON projects TO ingest_writer",
    "GRANT SELECT (id, plan) ON users TO ingest_writer",
    # INSERT ... ON CONFLICT DO UPDATE ... RETURNING needs all three.
    "GRANT SELECT, INSERT, UPDATE (calls, records) ON monthly_usage TO ingest_writer",
]

DOWNGRADE = [
    "DROP TABLE monthly_usage",
    "REVOKE SELECT (id, plan) ON users FROM ingest_writer",
    "REVOKE SELECT (id, owner_user_id, deleted_at) ON projects FROM ingest_writer",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
