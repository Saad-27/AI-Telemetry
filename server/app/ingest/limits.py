"""The per-key rate limit and the per-account monthly quota (brief §7.2, §9.2 step 3, ADR 0017).

Both are checked from memory, so a request costs no extra database call. The rate limit lives
only in this process. The quota is counted here and added to `monthly_usage` every FLUSH_S;
each flush returns the stored totals, which include other machines' counts.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from uuid import UUID

import asyncpg

from app.core.entitlements import Entitlements
from app.db import DB_TIMEOUT_S

log = logging.getLogger(__name__)

BUCKETS_MAX = 10_000  # like the key cache: one bucket per live key
FLUSH_S = 10.0

# Rows go in sorted (user, month) order, so two machines flushing at once lock rows in the
# same order and cannot deadlock.
_ADD = """
    INSERT INTO monthly_usage AS u (user_id, month, calls)
    SELECT * FROM unnest($1::uuid[], $2::date[], $3::bigint[])
    ON CONFLICT (user_id, month) DO UPDATE SET calls = u.calls + EXCLUDED.calls
    RETURNING user_id, month, calls
"""


class RateLimiter:
    """A token bucket per key, counted in records: `records_per_s`, bursts to `records_burst`.

    A request needs one token to start. Once its records are known they are charged too, which
    can leave the bucket in debt; the key then waits until it is back to one token. Each
    machine keeps its own buckets, so the limit applies per machine.
    """

    def __init__(
        self, clock: Callable[[], float] = time.monotonic, max_entries: int = BUCKETS_MAX
    ) -> None:
        self._clock = clock
        self._max_entries = max_entries
        # Key id -> (tokens, when counted). Least recently used first.
        self._buckets: dict[UUID, tuple[float, float]] = {}

    def wait_s(self, key_id: UUID, limits: Entitlements) -> float:
        """0 if the key may send now (and its request is charged one token), else the seconds
        until it may."""
        now = self._clock()
        tokens, then = self._buckets.pop(key_id, (limits.records_burst, now))
        tokens = min(limits.records_burst, tokens + (now - then) * limits.records_per_s)
        wait = 0.0 if tokens >= 1 else (1 - tokens) / limits.records_per_s
        if len(self._buckets) >= self._max_entries:
            del self._buckets[next(iter(self._buckets))]
        self._buckets[key_id] = (tokens - 1 if wait == 0 else tokens, now)
        return wait

    def charge(self, key_id: UUID, records: int) -> None:
        bucket = self._buckets.get(key_id)
        if bucket is not None:
            self._buckets[key_id] = (bucket[0] - records, bucket[1])


def _month(now: datetime) -> date:
    return date(now.year, now.month, 1)


def _next_month(month: date) -> datetime:
    return datetime(month.year + month.month // 12, month.month % 12 + 1, 1, tzinfo=UTC)


class Quota:
    """Calls measured per account per UTC month, against `monthly_calls`.

    An account is over once its stored total, as of this process's last flush, plus its calls
    not yet flushed reach the limit. Totals only grow within a month, so a stale one never
    blocks too early; it unblocks when the month ends or the plan's limit rises.
    """

    def __init__(self, pool: asyncpg.Pool, clock: Callable[[], float] = time.time) -> None:
        self._pool = pool
        self._clock = clock
        self._pending: dict[tuple[UUID, date], int] = {}
        self._stored: dict[UUID, tuple[date, int]] = {}  # user -> (month, total) at last flush

    def wait_s(self, user_id: UUID, limits: Entitlements) -> float:
        """0 if the account is under its quota, else the seconds until the quota resets."""
        now = datetime.fromtimestamp(self._clock(), UTC)
        month = _month(now)
        stored_month, stored = self._stored.get(user_id, (month, 0))
        used = (stored if stored_month == month else 0) + self._pending.get((user_id, month), 0)
        if used < limits.monthly_calls:
            return 0.0
        return (_next_month(month) - now).total_seconds()

    def add(self, user_id: UUID, calls: int) -> None:
        if calls:
            key = (user_id, _month(datetime.fromtimestamp(self._clock(), UTC)))
            self._pending[key] = self._pending.get(key, 0) + calls

    async def flush(self) -> None:
        """Adds the pending counts to the database. On failure they are kept for the next try,
        so a timeout after the commit counts them twice; that errs towards the limit."""
        if not self._pending:
            return  # no query: an idle database can sleep (ADR 0007)
        pending, self._pending = self._pending, {}
        keys = sorted(pending)
        try:
            async with self._pool.acquire(timeout=DB_TIMEOUT_S) as conn:
                rows = await conn.fetch(
                    _ADD,
                    [user for user, _ in keys],
                    [month for _, month in keys],
                    [pending[k] for k in keys],
                    timeout=DB_TIMEOUT_S,
                )
        except BaseException:
            for key, calls in pending.items():
                self._pending[key] = self._pending.get(key, 0) + calls
            raise
        for row in rows:
            known = self._stored.get(row["user_id"])
            if known is None or known[0] <= row["month"]:
                self._stored[row["user_id"]] = (row["month"], row["calls"])

    async def flush_logged(self) -> None:
        try:
            await self.flush()
        except Exception as exc:
            # Type only: driver messages can quote row values.
            log.warning("quota: flush failed (%s)", type(exc).__name__)

    async def flush_every(self, interval_s: float = FLUSH_S) -> None:
        while True:
            await asyncio.sleep(interval_s)
            await self.flush_logged()
