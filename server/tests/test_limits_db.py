"""Quota flushes into Postgres as ingest_writer, from more than one machine (ADR 0017)."""

import dataclasses
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import asyncpg
import pytest

from app.core.entitlements import FREE
from app.ingest.limits import Quota

pytestmark = [pytest.mark.db, pytest.mark.anyio]

LIMITS = dataclasses.replace(FREE, monthly_calls=100)
OCT = datetime(2026, 10, 17, tzinfo=UTC).timestamp()
NOV = datetime(2026, 11, 2, tzinfo=UTC).timestamp()


@pytest.fixture
async def owner(database_url: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(database_url)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def ingest_pool(database_url: str) -> AsyncIterator[asyncpg.Pool]:
    async def as_ingest_writer(conn: asyncpg.Connection) -> None:
        await conn.execute("SET ROLE ingest_writer")

    pool = await asyncpg.create_pool(database_url, min_size=0, max_size=2, setup=as_ingest_writer)
    try:
        yield pool
    finally:
        await pool.close()


async def add_user(owner: asyncpg.Connection) -> uuid.UUID:
    user: uuid.UUID = await owner.fetchval(
        "INSERT INTO users (email) VALUES ('q@example.com') RETURNING id"
    )
    return user


async def usage(owner: asyncpg.Connection, user: uuid.UUID) -> list[tuple[date, int]]:
    rows = await owner.fetch(
        "SELECT month, calls FROM monthly_usage WHERE user_id = $1 ORDER BY month", user
    )
    return [(r["month"], r["calls"]) for r in rows]


async def test_machines_add_up_and_learn_each_others_counts(
    owner: asyncpg.Connection, ingest_pool: asyncpg.Pool
) -> None:
    user, other = await add_user(owner), await add_user(owner)
    a = Quota(ingest_pool, clock=lambda: OCT)
    b = Quota(ingest_pool, clock=lambda: OCT)
    a.add(user, 60)
    a.add(other, 5)
    b.add(user, 30)
    await a.flush()
    assert b.wait_s(user, LIMITS) == 0  # b has seen only its own 30
    await b.flush()
    assert await usage(owner, user) == [(date(2026, 10, 1), 90)]
    await b.flush()  # nothing pending: no change
    assert await usage(owner, user) == [(date(2026, 10, 1), 90)]
    b.add(user, 10)
    assert b.wait_s(user, LIMITS) > 0  # 90 stored + 10 pending
    assert a.wait_s(user, LIMITS) == 0  # a last saw 60; it learns at its next flush


async def test_months_are_counted_apart(
    owner: asyncpg.Connection, ingest_pool: asyncpg.Pool
) -> None:
    now = [OCT]
    quota = Quota(ingest_pool, clock=lambda: now[0])
    user = await add_user(owner)
    quota.add(user, 100)
    now[0] = NOV
    quota.add(user, 7)
    await quota.flush()  # both months in one statement
    assert await usage(owner, user) == [(date(2026, 10, 1), 100), (date(2026, 11, 1), 7)]
    assert quota.wait_s(user, LIMITS) == 0


async def test_deleting_the_account_deletes_its_usage(
    owner: asyncpg.Connection, ingest_pool: asyncpg.Pool
) -> None:
    quota = Quota(ingest_pool, clock=lambda: OCT)
    user = await add_user(owner)
    quota.add(user, 1)
    await quota.flush()
    await owner.execute("DELETE FROM users WHERE id = $1", user)
    assert await usage(owner, user) == []


async def test_ingest_writer_reads_no_personal_data(owner: asyncpg.Connection) -> None:
    async with owner.transaction():
        await owner.execute("SET LOCAL ROLE ingest_writer")
        for query in ("SELECT email FROM users", "SELECT name FROM projects"):
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                async with owner.transaction():
                    await owner.fetch(query)
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await owner.execute("DELETE FROM monthly_usage")
