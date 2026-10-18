"""Building the dashboard's totals against Postgres (ADR 0023)."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest
from records import envelope, now_ms, summary

from app.db.partitions import ensure_partitions, maintain, partition_name
from app.db.totals import HOUR, build, built_through, ready_through
from app.ingest.schema import Envelope, validate_records
from app.ingest.sink import PostgresSink

pytestmark = [pytest.mark.db, pytest.mark.anyio]

# An hour boundary 2 to 3 hours ago: every bucket in it can be built.
START_MS = (now_ms() - 2 * 3_600_000) // 3_600_000 * 3_600_000
START = datetime.fromtimestamp(START_MS / 1000, UTC)
MINUTE = timedelta(minutes=1)

# Summed by hand from the summaries, for comparison with the totals.
_BY_HAND = """
    SELECT project_id, date_bin($2, minute, '2000-01-01T00:00:00+00') AS bucket, model,
           sum(count) AS count, sum(err_overloaded) AS overloaded,
           sum(ttft_hist[9]) AS ttft_9, sum(input_tokens_sum) AS tokens
    FROM summary_records WHERE project_id = ANY($1::uuid[]) GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
"""
_TOTALS = """
    SELECT project_id, bucket, model, sum(count) AS count, sum(err_overloaded) AS overloaded,
           sum(ttft_hist[9]) AS ttft_9, sum(input_tokens_sum) AS tokens
    FROM {table} WHERE project_id = ANY($1::uuid[]) GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
"""  # sum(): a bucket holds one row per series


@pytest.fixture
async def conn(database_url: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(database_url)
    await ensure_partitions(conn, datetime.now(UTC).date())
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def ingest_pool(database_url: str) -> AsyncIterator[asyncpg.Pool]:
    async def as_ingest_writer(conn: asyncpg.Connection) -> None:
        await conn.execute("SET ROLE ingest_writer")

    pool = await asyncpg.create_pool(database_url, min_size=0, max_size=1, setup=as_ingest_writer)
    try:
        yield pool
    finally:
        await pool.close()


def at(minute: int, **changes: Any) -> dict[str, Any]:
    return summary(minute=START_MS + minute * 60_000, **changes)


async def store(pool: asyncpg.Pool, project: uuid.UUID, *summaries: dict[str, Any]) -> None:
    records = validate_records(Envelope.model_validate(envelope(list(summaries))), now_ms())
    assert not records.errors, records.errors
    await PostgresSink(pool).write(project, records.summaries, [])


async def same_as_summaries(conn: asyncpg.Connection, projects: list[uuid.UUID]) -> None:
    for table, step in (("totals_5m", timedelta(minutes=5)), ("totals_1h", HOUR)):
        totals = await conn.fetch(_TOTALS.format(table=table), projects)
        assert totals == await conn.fetch(_BY_HAND, projects, step), table


async def test_totals_equal_the_summaries(
    conn: asyncpg.Connection, ingest_pool: asyncpg.Pool
) -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    failed = {"count": 3, "ok": 0, "cancelled": 0, "errors": {"overloaded": 3}}
    no_calls = {f"{h}_hist": [0] * n for h, n in (("ttft", 20), ("duration", 20), ("decode", 14))}
    # Minutes in START's hour only: the next hour may not be over yet.
    await store(ingest_pool, a, at(0), at(0), at(4), at(5), at(59, **failed, **no_calls))
    await store(ingest_pool, b, at(7), at(7, series={**at(0)["series"], "model": "m2"}))
    await conn.execute("UPDATE totals_watermark SET built_through = $1", START - HOUR)

    now = datetime.now(UTC)
    assert await build(conn, now) == ready_through(now) == await built_through(conn)
    await same_as_summaries(conn, [a, b])
    before = await conn.fetch(_TOTALS.format(table="totals_5m"), [a, b])
    assert await build(conn, now) == ready_through(now)  # nothing new: no change
    assert await conn.fetch(_TOTALS.format(table="totals_5m"), [a, b]) == before


async def test_late_summaries_are_rebuilt(
    conn: asyncpg.Connection, ingest_pool: asyncpg.Pool
) -> None:
    project = uuid.uuid4()
    await store(ingest_pool, project, at(10))
    await conn.execute("UPDATE totals_watermark SET built_through = $1", START - HOUR)
    await build(conn, datetime.now(UTC))
    # After the build, as from an SDK sending its backlog: its bucket and hour are marked.
    await store(ingest_pool, project, at(11), at(30))
    marks = await conn.fetch("SELECT bucket FROM totals_stale WHERE project_id = $1", project)
    assert sorted(m[0] for m in marks) == [START + 10 * MINUTE, START + 30 * MINUTE]
    await build(conn, datetime.now(UTC))
    await same_as_summaries(conn, [project])
    assert not await conn.fetch("SELECT 1 FROM totals_stale WHERE project_id = $1", project)


async def test_recent_summaries_are_not_marked(
    conn: asyncpg.Connection, ingest_pool: asyncpg.Pool
) -> None:
    project = uuid.uuid4()
    await store(ingest_pool, project, summary())  # the previous minute: not built yet
    assert not await conn.fetch("SELECT 1 FROM totals_stale WHERE project_id = $1", project)


async def test_one_machine_builds_at_a_time(conn: asyncpg.Connection, database_url: str) -> None:
    other = await asyncpg.connect(database_url)
    try:
        await other.execute("SELECT pg_advisory_lock(20023)")
        assert await build(conn, datetime.now(UTC)) is None
    finally:
        await other.close()


async def test_a_long_pause_is_left_to_the_daily_upkeep(conn: asyncpg.Connection) -> None:
    now = datetime.now(UTC)
    days_ago = datetime.combine(now.date() - timedelta(days=3), datetime.min.time(), UTC)
    await conn.execute("UPDATE totals_watermark SET built_through = $1", days_ago)
    # Its totals partitions may not exist: only the upkeep makes them.
    assert await build(conn, now) == days_ago
    assert await maintain(conn, now, keep_days=7)
    assert await built_through(conn) == ready_through(now)
    hourly = partition_name("totals_1h", days_ago.date())
    assert await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", hourly)


async def test_dashboard_app_sees_one_project(
    conn: asyncpg.Connection, ingest_pool: asyncpg.Pool, database_url: str
) -> None:
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    await store(ingest_pool, mine, at(20))
    await store(ingest_pool, theirs, at(20))
    await conn.execute("UPDATE totals_watermark SET built_through = $1", START - HOUR)
    await build(conn, datetime.now(UTC))
    dashboard = await asyncpg.connect(database_url)
    try:
        await dashboard.execute("SET ROLE dashboard_app")
        async with dashboard.transaction():
            await dashboard.execute("SELECT set_config('app.project_id', $1, true)", str(mine))
            for table in ("totals_5m", "totals_1h"):
                # No WHERE clause: row-level security alone keeps the other project out.
                seen = await dashboard.fetch(f"SELECT DISTINCT project_id FROM {table}")  # noqa: S608
                assert [r[0] for r in seen] == [mine], table
    finally:
        await dashboard.close()
