"""Ingest keys: format, checksum, hashing and the cached lookup (brief §11.4, ADR 0015).

A key is `gl_live_`, 32 random bytes in base62 (43 characters), then the CRC32 of everything
before it in base62 (6 characters), 57 characters in all. The checksum lets ingest reject a
mistyped or made-up key without a database lookup. Only the key's SHA-256 is stored.
"""

import hashlib
import re
import secrets
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID

import asyncpg

from app.db import DB_TIMEOUT_S

PREFIX = "gl_live_"
_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
BODY_LEN = 43  # 62**43 > 2**256
CHECK_LEN = 6  # 62**6 > 2**32
LENGTH = len(PREFIX) + BODY_LEN + CHECK_LEN
_SHAPE = re.compile(f"{PREFIX}[0-9A-Za-z]{{{BODY_LEN + CHECK_LEN}}}")

CACHE_TTL_S = 60.0  # a revoked key stops working within this (brief §11.4)
CACHE_MAX = 10_000

# One round trip finds a live key and marks it used, so `last_used_at` is written at most once
# per key per cache period (brief §9.2 step 7). Unknown and revoked keys match no row.
_TOUCH = """
    UPDATE ingest_keys SET last_used_at = now()
    WHERE key_hash = $1 AND revoked_at IS NULL
    RETURNING id, project_id
"""


def _base62(n: int, width: int) -> str:
    digits = []
    for _ in range(width):
        n, rem = divmod(n, 62)
        digits.append(_ALPHABET[rem])
    return "".join(reversed(digits))


def _checksum(text: str) -> str:
    return _base62(zlib.crc32(text.encode("ascii")), CHECK_LEN)


def hash_key(key: str) -> bytes:
    # A fast hash suits a 256-bit random key; bcrypt would add no security (brief §11.4).
    return hashlib.sha256(key.encode("ascii")).digest()


def well_formed(key: str) -> bool:
    """Prefix, length, alphabet and checksum. Needs no database."""
    return (
        len(key) == LENGTH
        and _SHAPE.fullmatch(key) is not None
        and _checksum(key[:-CHECK_LEN]) == key[-CHECK_LEN:]
    )


@dataclass(frozen=True, slots=True)
class NewKey:
    """`key` is shown to the user once; the other fields are what gets stored."""

    key: str = field(repr=False)
    key_hash: bytes
    display_prefix: str
    last4: str


def generate() -> NewKey:
    body = PREFIX + _base62(secrets.randbits(256), BODY_LEN)
    key = body + _checksum(body)
    return NewKey(key, hash_key(key), key[: len(PREFIX) + 4], key[-4:])


@dataclass(frozen=True, slots=True)
class IngestKey:
    id: UUID
    project_id: UUID


class KeyStore:
    """Resolves keys to their project and caches each key found for CACHE_TTL_S.

    Keys not found are not cached: the SDK stops sending after a 401, so they rarely repeat,
    and made-up keys cannot push real ones out of the cache.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        ttl_s: float = CACHE_TTL_S,
        max_entries: int = CACHE_MAX,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._pool = pool
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._clock = clock
        # Key hash -> (expiry, key). Oldest first, so eviction pops the first entry.
        self._cache: dict[bytes, tuple[float, IngestKey]] = {}

    async def resolve(self, key: str) -> IngestKey | None:
        """The key's project, or None if the key is malformed, unknown or revoked.

        Database errors propagate and are not cached: an outage must not look like a bad key.
        """
        if not well_formed(key):
            return None
        key_hash = hash_key(key)
        now = self._clock()
        cached = self._cache.get(key_hash)
        if cached is not None and cached[0] > now:
            return cached[1]
        async with self._pool.acquire(timeout=DB_TIMEOUT_S) as conn:
            row = await conn.fetchrow(_TOUCH, key_hash, timeout=DB_TIMEOUT_S)
        self._cache.pop(key_hash, None)
        if row is None:
            return None
        found = IngestKey(row["id"], row["project_id"])
        if len(self._cache) >= self._max_entries:
            del self._cache[next(iter(self._cache))]
        # Expiry counts from before the query, so a revocation is never served for longer.
        self._cache[key_hash] = (now + self._ttl_s, found)
        return found
