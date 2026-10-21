"""Partition upkeep and retention against Postgres (ADR 0020). Creation is in test_schema.py;
the hourly schedule in test_retention.py."""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta

import asyncpg
import pytest

from app.core.config import Settings
from app.db.partitions import (
    DAYS_AHEAD,
    PARTITIONED,
    drop_expired,
    ensure_partitions,
    maintain,
    partition_name,
    retention,
)
from app.worker import run

pytestmark = pytest.mark.db

# Long past, so the current partitions other tests use are never touched.
DAY = date(2001, 3, 10)
NOON = datetime(2001, 3, 10, 12, tzinfo=UTC)
TIDY = dict.fromkeys(PARTITIONED, DAY + timedelta(days=13))  # drops every day these tests make


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


def names(*days: date, tables: tuple[str, ...] = tuple(PARTITIONED)) -> set[str]:
    return {partition_name(table, day) for table in tables for day in days}


def days(first: int, last: int) -> list[date]:
    """DAY + first to DAY + last days, both included."""
    return [DAY + timedelta(days=i) for i in range(first, last + 1)]


DETAIL = ("summary_records", "totals_5m")
KEPT = ("call_samples", "totals_1h")


@pytest.mark.anyio
async def test_days_past_retention_are_dropped(conn: asyncpg.Connection) -> None:
    await ensure_partitions(conn, DAY - timedelta(days=8), days_ahead=9)  # Mar 1 to Mar 11
    await conn.execute(
        "CREATE TABLE summary_records_p2001_old PARTITION OF summary_records "
        "FOR VALUES FROM ('2001-01-01T00:00:00+00') TO ('2001-01-02T00:00:00+00')"
    )

    # Totals are built through today, so only the retention decides.
    dropped = await drop_expired(conn, retention(DAY, 7, built=NOON))

    # Samples and hourly totals: Mar 1 and 2 hold only rows over 7 days old. Summaries and
    # 5-minute totals: everything before Mar 8, two days before.
    old = names(*days(-9, -8), tables=KEPT) | names(*days(-9, -3), tables=DETAIL)
    assert set(dropped) == old
    left = await existing(conn)
    assert not old & left
    assert names(*days(-7, 1), tables=KEPT) | names(*days(-2, 1), tables=DETAIL) <= left
    assert "summary_records_p2001_old" in left  # not named like ours: left alone
    await conn.execute("DROP TABLE summary_records_p2001_old")
    await drop_expired(conn, TIDY)


@pytest.mark.anyio
async def test_summaries_not_yet_in_totals_are_kept(conn: asyncpg.Connection) -> None:
    await ensure_partitions(conn, DAY - timedelta(days=5), days_ahead=0)  # Mar 4 and 5
    built = NOON - timedelta(days=6)  # Mar 4: no build since then
    dropped = await drop_expired(conn, retention(DAY, 7, built))
    assert not set(dropped) & names(*days(-6, -5), tables=("summary_records",))
    assert names(*days(-6, -5), tables=("totals_5m",)) <= set(dropped)
    await drop_expired(conn, TIDY)


@pytest.mark.anyio
async def test_one_machine_at_a_time(conn: asyncpg.Connection, database_url: str) -> None:
    other = await asyncpg.connect(database_url)
    try:
        await other.execute("SELECT pg_advisory_lock(20020)")  # another machine is at it
        assert not await maintain(conn, NOON, keep_days=7)
        assert not names(DAY) & await existing(conn)
        await other.execute("SELECT pg_advisory_unlock(20020)")
        assert await maintain(conn, NOON, keep_days=7)
        assert names(DAY, DAY + timedelta(days=DAYS_AHEAD)) <= await existing(conn)
        # The lock was released: the other machine gets it now.
        assert await other.fetchval("SELECT pg_try_advisory_lock(20020)")
    finally:
        await other.close()
    await drop_expired(conn, TIDY)


@pytest.mark.anyio
async def test_the_worker_makes_partitions_when_it_starts(
    database_url: str, conn: asyncpg.Connection
) -> None:
    last = datetime.now(UTC).date() + timedelta(days=DAYS_AHEAD)  # holds no rows yet
    for table in PARTITIONED:
        await conn.execute(f"DROP TABLE IF EXISTS {partition_name(table, last)}")
    worker = asyncio.create_task(run(Settings(database_url=database_url)))
    try:
        deadline = time.monotonic() + 5
        while not names(last) <= await existing(conn):
            assert time.monotonic() < deadline, "partitions not made at startup"
            await asyncio.sleep(0.05)
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
