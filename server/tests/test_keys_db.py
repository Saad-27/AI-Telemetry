"""The key lookup against Postgres, connected as ingest_writer (ADR 0013, ADR 0015)."""

from collections.abc import AsyncIterator

import asyncpg
import pytest

from app.ingest.keys import IngestKey, KeyStore, NewKey, generate

pytestmark = [pytest.mark.db, pytest.mark.anyio]


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

    pool = await asyncpg.create_pool(database_url, min_size=0, max_size=1, setup=as_ingest_writer)
    try:
        yield pool
    finally:
        await pool.close()


async def add_key(owner: asyncpg.Connection) -> tuple[NewKey, IngestKey]:
    new = generate()
    user = await owner.fetchval("INSERT INTO users (email) VALUES ('a@example.com') RETURNING id")
    project = await owner.fetchval(
        "INSERT INTO projects (name, owner_user_id) VALUES ('p', $1) RETURNING id", user
    )
    key_id = await owner.fetchval(
        "INSERT INTO ingest_keys (project_id, key_hash, display_prefix, last4, created_by) "
        "VALUES ($1, $2, $3, $4, $5) RETURNING id",
        project,
        new.key_hash,
        new.display_prefix,
        new.last4,
        user,
    )
    return new, IngestKey(key_id, project, user, "free")


async def test_resolves_and_marks_used(
    owner: asyncpg.Connection, ingest_pool: asyncpg.Pool
) -> None:
    new, expected = await add_key(owner)
    assert await KeyStore(ingest_pool).resolve(new.key) == expected
    used = await owner.fetchval("SELECT last_used_at FROM ingest_keys WHERE id = $1", expected.id)
    assert used is not None


async def test_unknown_key(ingest_pool: asyncpg.Pool) -> None:
    assert await KeyStore(ingest_pool).resolve(generate().key) is None


async def test_revocation_takes_effect_when_the_cache_expires(
    owner: asyncpg.Connection, ingest_pool: asyncpg.Pool
) -> None:
    now = [0.0]
    store = KeyStore(ingest_pool, ttl_s=60, clock=lambda: now[0])
    new, expected = await add_key(owner)
    assert await store.resolve(new.key) == expected
    await owner.execute("UPDATE ingest_keys SET revoked_at = now() WHERE id = $1", expected.id)
    assert await store.resolve(new.key) == expected  # cached
    now[0] = 60.0
    assert await store.resolve(new.key) is None


async def test_keys_of_deleted_projects_are_refused(
    owner: asyncpg.Connection, ingest_pool: asyncpg.Pool
) -> None:
    new, expected = await add_key(owner)
    await owner.execute("UPDATE projects SET deleted_at = now() WHERE id = $1", expected.project_id)
    assert await KeyStore(ingest_pool).resolve(new.key) is None


async def test_plan_changes_apply_when_the_cache_expires(
    owner: asyncpg.Connection, ingest_pool: asyncpg.Pool
) -> None:
    now = [0.0]
    store = KeyStore(ingest_pool, ttl_s=60, clock=lambda: now[0])
    new, expected = await add_key(owner)
    assert await store.resolve(new.key) == expected
    await owner.execute("UPDATE users SET plan = 'pro' WHERE id = $1", expected.user_id)
    now[0] = 60.0
    found = await store.resolve(new.key)
    assert found is not None and found.plan == "pro"


async def test_lookup_returns_this_months_usage(
    owner: asyncpg.Connection, ingest_pool: asyncpg.Pool
) -> None:
    learned: list[tuple[object, ...]] = []
    store = KeyStore(ingest_pool, learn=lambda *usage: learned.append(usage))
    new, expected = await add_key(owner)
    month = await owner.fetchval("SELECT date_trunc('month', now() AT TIME ZONE 'UTC')::date")
    await owner.execute(
        "INSERT INTO monthly_usage (user_id, month, calls, records) "
        "VALUES ($1, $2, 7, 11), ($1, $2 - interval '1 month', 99, 99)",
        expected.user_id,
        month,
    )
    assert await store.resolve(new.key) == expected
    assert learned == [(expected.user_id, month, 7, 11)]
