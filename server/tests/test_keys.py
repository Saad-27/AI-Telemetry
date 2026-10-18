"""Ingest key format, checksum and the cached lookup. The real query is in test_keys_db.py."""

import hashlib
import uuid
from datetime import date
from types import TracebackType
from typing import Any

import pytest
from prometheus_client import REGISTRY

from app.ingest.keys import (
    BODY_LEN,
    CHECK_LEN,
    LENGTH,
    PREFIX,
    IngestKey,
    KeyStore,
    generate,
    hash_key,
    well_formed,
)

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
KEY = generate()


def test_encoding_widths_fit_their_values() -> None:
    assert 62**BODY_LEN >= 2**256
    assert 62**CHECK_LEN >= 2**32
    assert LENGTH == 57


def test_generated_key_shape() -> None:
    key = KEY.key
    assert len(key) == LENGTH and key.startswith(PREFIX)
    assert well_formed(key)
    assert KEY.key_hash == hashlib.sha256(key.encode()).digest() == hash_key(key)
    assert KEY.display_prefix == key[:12] and KEY.last4 == key[-4:]
    # The SDK sends only keys that pass its own check (sdk/python/src/gaslit/_config.py).
    assert key.startswith("gl_") and key.isascii() and key.replace("_", "").isalnum()


def test_new_key_repr_hides_the_key() -> None:
    assert KEY.key not in repr(KEY)


def test_every_single_character_change_is_caught() -> None:
    key = KEY.key
    for i in range(len(PREFIX), LENGTH):
        for char in ALPHABET.replace(key[i], ""):
            assert not well_formed(key[:i] + char + key[i + 1 :])


@pytest.mark.parametrize(
    "key",
    [
        "",
        PREFIX,
        KEY.key[:-1],
        KEY.key + "0",
        KEY.key + "\n",
        " " + KEY.key[1:],
        "gl_test_" + KEY.key[len(PREFIX) :],
        KEY.key[:20] + "٣" + KEY.key[21:],  # an Arabic-Indic digit
        "Bearer " + KEY.key,
        "sk-" + "a" * 54,
    ],
)
def test_malformed_keys(key: str) -> None:
    assert not well_formed(key)


class FakePool:
    """Answers the lookup from `rows` and counts round trips."""

    def __init__(self, rows: dict[bytes, dict[str, Any]]) -> None:
        self.rows = rows
        self.queries = 0
        self.error: Exception | None = None

    def acquire(self, *, timeout: float) -> "FakePool":
        return self

    async def __aenter__(self) -> "FakePool":
        return self

    async def __aexit__(
        self, typ: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        return None

    async def fetchrow(self, query: str, key_hash: bytes, *, timeout: float) -> Any:
        self.queries += 1
        if self.error:
            raise self.error
        return self.rows.get(key_hash)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def found(usage: tuple[date, int, int] | None = None) -> dict[str, Any]:
    month, calls, records = usage or (None, None, None)
    return {
        "id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "plan": "free",
        "month": month,
        "calls": calls,
        "records": records,
    }


def key_of(row: dict[str, Any]) -> IngestKey:
    return IngestKey(row["id"], row["project_id"], row["user_id"], row["plan"])


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def pool() -> FakePool:
    return FakePool({KEY.key_hash: found()})


@pytest.mark.anyio
async def test_malformed_key_never_reaches_the_database(pool: FakePool) -> None:
    store = KeyStore(pool)
    assert await store.resolve("gl_live_" + "0" * 49) is None
    assert pool.queries == 0


@pytest.mark.anyio
async def test_found_key_is_cached_until_expiry(pool: FakePool, clock: Clock) -> None:
    store = KeyStore(pool, ttl_s=60, clock=clock)
    expected = key_of(pool.rows[KEY.key_hash])
    assert await store.resolve(KEY.key) == expected
    del pool.rows[KEY.key_hash]  # revoked
    clock.now += 59.9
    assert await store.resolve(KEY.key) == expected
    assert pool.queries == 1
    clock.now += 0.1
    assert await store.resolve(KEY.key) is None
    assert pool.queries == 2


@pytest.mark.anyio
async def test_unknown_key_is_not_cached(pool: FakePool) -> None:
    def lookups(result: str) -> float:
        return REGISTRY.get_sample_value("gaslit_key_lookups_total", {"result": result}) or 0.0

    before = lookups("found"), lookups("missing")
    store = KeyStore(pool)
    other = generate()
    assert await store.resolve(other.key) is None
    pool.rows[other.key_hash] = found()  # created after the first try
    assert await store.resolve(other.key) is not None
    assert await store.resolve(other.key) is not None  # cached: not a lookup
    assert pool.queries == 2
    assert (lookups("found") - before[0], lookups("missing") - before[1]) == (1, 1)


@pytest.mark.anyio
async def test_cache_is_bounded_and_drops_the_oldest(pool: FakePool) -> None:
    store = KeyStore(pool, max_entries=2)
    keys = [KEY, generate(), generate()]
    for k in keys[1:]:
        pool.rows[k.key_hash] = found()
    for k in keys:
        await store.resolve(k.key)
    assert pool.queries == 3
    await store.resolve(keys[2].key)
    await store.resolve(keys[1].key)
    assert pool.queries == 3
    await store.resolve(keys[0].key)  # evicted
    assert pool.queries == 4


@pytest.mark.anyio
async def test_database_errors_propagate_and_are_not_cached(pool: FakePool) -> None:
    store = KeyStore(pool)
    pool.error = TimeoutError()
    with pytest.raises(TimeoutError):
        await store.resolve(KEY.key)
    pool.error = None
    assert await store.resolve(KEY.key) is not None
    assert pool.queries == 2


@pytest.mark.anyio
async def test_expired_entry_is_served_while_keep_holds_it(pool: FakePool, clock: Clock) -> None:
    held: set[IngestKey] = set()
    store = KeyStore(pool, ttl_s=60, clock=clock, keep=held.__contains__)
    expected = key_of(pool.rows[KEY.key_hash])
    assert await store.resolve(KEY.key) == expected
    held.add(expected)  # its account went over quota
    del pool.rows[KEY.key_hash]
    clock.now += 3600
    assert await store.resolve(KEY.key) == expected
    assert pool.queries == 1
    held.clear()  # a new month, or the upgrade flow called Quota.unblock
    assert await store.resolve(KEY.key) is None
    assert pool.queries == 2


@pytest.mark.anyio
async def test_lookups_pass_this_months_usage_on(pool: FakePool, clock: Clock) -> None:
    learned: list[tuple[uuid.UUID, date, int, int]] = []
    store = KeyStore(pool, ttl_s=60, clock=clock, learn=lambda *usage: learned.append(usage))
    await store.resolve(KEY.key)  # no usage yet this month
    assert learned == []
    pool.rows[KEY.key_hash] |= {"month": date(2026, 10, 1), "calls": 5, "records": 9}
    await store.resolve(KEY.key)  # cached: nothing new
    clock.now += 60
    await store.resolve(KEY.key)
    assert learned == [(pool.rows[KEY.key_hash]["user_id"], date(2026, 10, 1), 5, 9)]
