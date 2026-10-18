"""Partition upkeep and retention against Postgres (ADR 0020). Creation is in test_schema.py;
the daily schedule in test_retention.py."""

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.partitions import (
    DAYS_AHEAD,
    PARTITIONED,
    drop_expired,
    ensure_partitions,
    maintain,
    partition_name,
)
from app.main import create_app

pytestmark = pytest.mark.db

# Long past, so the current partitions other tests use are never touched.
DAY = date(2001, 3, 10)


@pytest.fixture
async def conn(database_url: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(database_url)
    try:
        yield conn
    finally:
        await conn.close()


async def existing(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch(
        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
        "WHERE i.inhparent = ANY($1::regclass[])",
        list(PARTITIONED),
    )
    return {r["relname"] for r in rows}


def names(*days: date) -> set[str]:
    return {partition_name(table, day) for table in PARTITIONED for day in days}


@pytest.mark.anyio
async def test_days_past_retention_are_dropped(conn: asyncpg.Connection) -> None:
    await ensure_partitions(conn, DAY - timedelta(days=8), days_ahead=9)  # Mar 1 to Mar 11
    await conn.execute(
        "CREATE TABLE summary_records_p2001_old PARTITION OF summary_records "
        "FOR VALUES FROM ('2001-01-01T00:00:00+00') TO ('2001-01-02T00:00:00+00')"
    )
    old = [DAY - timedelta(days=9), DAY - timedelta(days=8)]  # Mar 1 and 2: all rows over 7 days
    kept = [DAY - timedelta(days=7) + timedelta(days=i) for i in range(9)]  # Mar 3 to Mar 11

    dropped = await drop_expired(conn, DAY, keep_days=7)

    assert set(dropped) == names(*old)
    left = await existing(conn)
    assert not names(*old) & left
    assert names(*kept) <= left
    assert "summary_records_p2001_old" in left  # not named like ours: left alone
    await conn.execute("DROP TABLE summary_records_p2001_old")
    await drop_expired(conn, DAY + timedelta(days=20), keep_days=7)  # tidy up


@pytest.mark.anyio
async def test_one_machine_at_a_time(conn: asyncpg.Connection, database_url: str) -> None:
    other = await asyncpg.connect(database_url)
    try:
        await other.execute("SELECT pg_advisory_lock(20020)")  # another machine is at it
        assert not await maintain(conn, DAY, keep_days=7)
        assert not names(DAY) & await existing(conn)
        await other.execute("SELECT pg_advisory_unlock(20020)")
        assert await maintain(conn, DAY, keep_days=7)
        assert names(DAY, DAY + timedelta(days=DAYS_AHEAD)) <= await existing(conn)
        # The lock was released: the other machine gets it now.
        assert await other.fetchval("SELECT pg_try_advisory_lock(20020)")
    finally:
        await other.close()
    await drop_expired(conn, DAY + timedelta(days=20), keep_days=7)


def test_the_app_makes_partitions_when_it_starts(database_url: str) -> None:
    today = datetime.now(UTC).date()
    last = today + timedelta(days=DAYS_AHEAD)  # holds no rows yet

    async def run(query: str) -> set[str]:
        conn = await asyncpg.connect(database_url)
        try:
            await conn.execute(query)
            return await existing(conn)
        finally:
            await conn.close()

    for table in PARTITIONED:
        asyncio.run(run(f"DROP TABLE IF EXISTS {partition_name(table, last)}"))
    with TestClient(create_app(Settings(database_url=database_url))):
        deadline = time.monotonic() + 5
        while not names(last) <= asyncio.run(run("SELECT 1")):
            assert time.monotonic() < deadline, "partitions not made at startup"
            time.sleep(0.05)
