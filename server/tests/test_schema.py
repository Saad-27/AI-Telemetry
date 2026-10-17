"""The first migration: partitions, idempotent inserts, roles and row-level security."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

from app.db.partitions import DAYS_AHEAD, PARTITIONED, ensure_partitions, partition_name

pytestmark = [pytest.mark.db, pytest.mark.anyio]

TODAY = datetime.now(UTC).date()
NOW = datetime.now(UTC).replace(second=0, microsecond=0)


@pytest.fixture
async def conn(database_url: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(database_url)
    await ensure_partitions(conn, TODAY)
    try:
        yield conn
    finally:
        await conn.close()


async def insert_summary(
    conn: asyncpg.Connection,
    project_id: uuid.UUID,
    summary_id: uuid.UUID | None = None,
    minute: datetime = NOW,
) -> str:
    return str(
        await conn.execute(
            """
            INSERT INTO summary_records (
                project_id, minute, summary_id, provider, route, operation, model, streaming,
                input_bucket, count, ok, cancelled, ttft_hist, duration_hist, decode_hist,
                input_tokens_sum, output_tokens_sum, output_tokens_estimated_count
            ) VALUES ($1, $2, $3, 'openai', 'direct', 'responses', 'gpt-x', false, 's',
                      1, 1, 0, $4, $4, $5, 10, 5, 0)
            ON CONFLICT DO NOTHING
            """,
            project_id,
            minute,
            summary_id or uuid.uuid4(),
            [0] * 20,
            [0] * 14,
        )
    )


async def partitions(conn: asyncpg.Connection, table: str) -> set[str]:
    rows = await conn.fetch(
        "SELECT c.relname FROM pg_inherits i "
        "JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = $1::regclass",
        table,
    )
    return {r["relname"] for r in rows}


async def test_partitions_cover_yesterday_to_days_ahead(conn: asyncpg.Connection) -> None:
    await ensure_partitions(conn, TODAY)  # second run is a no-op
    for table in PARTITIONED:
        names = await partitions(conn, table)
        for offset in range(-1, DAYS_AHEAD + 1):
            assert partition_name(table, TODAY + timedelta(days=offset)) in names


async def test_partition_bounds_are_utc_days(conn: asyncpg.Connection) -> None:
    await conn.execute("SET TIME ZONE 'Pacific/Kiritimati'")  # UTC+14
    day = date(2000, 1, 1)
    await ensure_partitions(conn, day, days_ahead=0)
    project = uuid.uuid4()
    midnight = datetime(2000, 1, 1, tzinfo=UTC)
    await insert_summary(conn, project, minute=midnight)
    await insert_summary(conn, project, minute=midnight + timedelta(days=1) - timedelta(minutes=1))
    count = await conn.fetchval(
        f"SELECT count(*) FROM {partition_name('summary_records', day)} WHERE project_id = $1",  # noqa: S608
        project,
    )
    assert count == 2


async def test_insert_without_partition_fails(conn: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError):  # "no partition ... found"
        await insert_summary(conn, uuid.uuid4(), minute=NOW + timedelta(days=DAYS_AHEAD + 2))


async def test_ingest_writer_appends_idempotently(conn: asyncpg.Connection) -> None:
    project, summary = uuid.uuid4(), uuid.uuid4()
    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE ingest_writer")
        assert await insert_summary(conn, project, summary) == "INSERT 0 1"
        assert await insert_summary(conn, project, summary) == "INSERT 0 0"  # SDK retry


async def test_ingest_writer_cannot_read_data(conn: asyncpg.Connection) -> None:
    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE ingest_writer")
        for query in (
            "SELECT 1 FROM summary_records",
            "SELECT 1 FROM call_samples",
            "SELECT 1 FROM users",
            "SELECT display_prefix FROM ingest_keys",
        ):
            # Each in a savepoint that rolls back, so the outer transaction survives.
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                async with conn.transaction():
                    await conn.fetch(query)


async def test_copy_is_refused_under_row_level_security(conn: asyncpg.Connection) -> None:
    # Why ingest batches with INSERT ... SELECT FROM unnest() instead of COPY (ADR 0013).
    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE ingest_writer")
        with pytest.raises(asyncpg.exceptions.FeatureNotSupportedError):
            await conn.copy_records_to_table(
                "call_samples", records=[], columns=["project_id", "ts", "event_id"]
            )


async def test_dashboard_sees_only_the_current_project(conn: asyncpg.Connection) -> None:
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    await insert_summary(conn, mine)
    await insert_summary(conn, theirs)
    query = "SELECT DISTINCT project_id FROM summary_records WHERE project_id = ANY($1)"
    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE dashboard_app")
        await conn.execute("SELECT set_config('app.project_id', $1, true)", str(mine))
        assert await conn.fetch(query, [mine, theirs]) == [(mine,)]
    # After the transaction the setting is '' rather than unset; still no rows, no error.
    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE dashboard_app")
        assert await conn.fetch(query, [mine, theirs]) == []


async def test_dashboard_cannot_write_data(conn: asyncpg.Connection) -> None:
    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE dashboard_app")
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await insert_summary(conn, uuid.uuid4())


def test_downgrade_and_upgrade_again(alembic_config: Config) -> None:
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")
