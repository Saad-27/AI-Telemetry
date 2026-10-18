"""The per-account rate limit and monthly quota (brief §7.2, §9.2 step 3, ADR 0017, ADR 0019).

Both are checked from memory, so a request costs no extra database call. The rate limit lives
only in this process. The quota is counted here and added to `monthly_usage` every FLUSH_S;
each flush, and each key lookup, returns the stored totals, which include other machines'
counts.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from uuid import UUID

import asyncpg

from app.core.entitlements import Entitlements, entitlements
from app.db import DB_TIMEOUT_S
from app.ingest.keys import IngestKey

log = logging.getLogger(__name__)

BUCKETS_MAX = 10_000  # one bucket per account sending now
FLUSH_S = 10.0

# Rows go in sorted (user, month) order, so two machines flushing at once lock rows in the
# same order and cannot deadlock.
_ADD = """
    INSERT INTO monthly_usage AS u (user_id, month, calls, records)
    SELECT * FROM unnest($1::uuid[], $2::date[], $3::bigint[], $4::bigint[])
    ON CONFLICT (user_id, month) DO UPDATE
    SET calls = u.calls + EXCLUDED.calls, records = u.records + EXCLUDED.records
    RETURNING user_id, month, calls, records
"""


class RateLimiter:
    """A token bucket per account, counted in records: `records_per_s`, bursts to
    `records_burst`.

    Per account, not per key, because keys are free to create. A request needs one token to
    start. Once its records are known they are charged too, which can leave the bucket in
    debt; the account then waits until it is back to one token. Each machine keeps its own
    buckets, so the limit applies per machine.
    """

    def __init__(
        self, clock: Callable[[], float] = time.monotonic, max_entries: int = BUCKETS_MAX
    ) -> None:
        self._clock = clock
        self._max_entries = max_entries
        # Account -> (tokens, when counted). Least recently used first.
        self._buckets: dict[UUID, tuple[float, float]] = {}

    def wait_s(self, user_id: UUID, limits: Entitlements) -> float:
        """0 if the account may send now (and its request is charged one token), else the
        seconds until it may."""
        now = self._clock()
        tokens, then = self._buckets.pop(user_id, (limits.records_burst, now))
        tokens = min(limits.records_burst, tokens + (now - then) * limits.records_per_s)
        wait = 0.0 if tokens >= 1 else (1 - tokens) / limits.records_per_s
        if len(self._buckets) >= self._max_entries:
            del self._buckets[next(iter(self._buckets))]
        self._buckets[user_id] = (tokens - 1 if wait == 0 else tokens, now)
        return wait

    def charge(self, user_id: UUID, records: int) -> None:
        bucket = self._buckets.get(user_id)
        if bucket is not None:
            self._buckets[user_id] = (bucket[0] - records, bucket[1])


def _month(now: datetime) -> date:
    return date(now.year, now.month, 1)


def _next_month(month: date) -> datetime:
    return datetime(month.year + month.month // 12, month.month % 12 + 1, 1, tzinfo=UTC)


class Quota:
    """Calls measured and records stored per account per UTC month, against `monthly_calls`
    and `monthly_records`.

    An account is over once a stored total, as of this process's last flush or key lookup,
    plus the counts not yet flushed reach either limit. Totals only grow within a month, so a
    stale one never blocks too early. It unblocks when the month ends, or when `unblock` is
    called and the next key lookup returns higher limits (ADR 0019).
    """

    def __init__(self, pool: asyncpg.Pool, clock: Callable[[], float] = time.time) -> None:
        self._pool = pool
        self._clock = clock
        self._pending: dict[tuple[UUID, date], tuple[int, int]] = {}  # (calls, records)
        # User -> (month, calls, records) at the last flush.
        self._stored: dict[UUID, tuple[date, int, int]] = {}

    def wait_s(self, user_id: UUID, limits: Entitlements) -> float:
        """0 if the account is under its quota, else the seconds until the quota resets."""
        now = datetime.fromtimestamp(self._clock(), UTC)
        month = _month(now)
        stored_month, calls, records = self._stored.get(user_id, (month, 0, 0))
        if stored_month != month:
            calls = records = 0
        pending_calls, pending_records = self._pending.get((user_id, month), (0, 0))
        if (
            calls + pending_calls < limits.monthly_calls
            and records + pending_records < limits.monthly_records
        ):
            return 0.0
        return (_next_month(month) - now).total_seconds()

    def blocks(self, key: IngestKey) -> bool:
        """Whether the key's account is over quota. While it is, the key store serves the key
        from memory past its expiry, so the account's checks never reach the database."""
        return self.wait_s(key.user_id, entitlements(key.plan)) > 0

    def learn(self, user_id: UUID, month: date, calls: int, records: int) -> None:
        """Stored totals, from a flush or the key lookup. They only grow within a month, so the
        larger of two answers for the same month is the newer one."""
        known = self._stored.get(user_id)
        if known is None or known[0] < month:
            self._stored[user_id] = (month, calls, records)
        elif known[0] == month:
            self._stored[user_id] = (month, max(known[1], calls), max(known[2], records))

    def unblock(self, user_id: UUID) -> None:
        """Forgets the account's totals, so its next request looks its key up again and is
        checked against its plan's limits as they are now. The upgrade flow (M4 or the paid
        tier) must call this on every ingest machine: until then, an account over quota keeps
        its old plan until the month ends."""
        self._stored.pop(user_id, None)

    def add(self, user_id: UUID, calls: int, records: int) -> None:
        if calls or records:
            month = _month(datetime.fromtimestamp(self._clock(), UTC))
            self._add_pending((user_id, month), calls, records)

    def _add_pending(self, key: tuple[UUID, date], calls: int, records: int) -> None:
        old_calls, old_records = self._pending.get(key, (0, 0))
        self._pending[key] = (old_calls + calls, old_records + records)

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
                    [pending[k][0] for k in keys],
                    [pending[k][1] for k in keys],
                    timeout=DB_TIMEOUT_S,
                )
        except BaseException:
            for key, (calls, records) in pending.items():
                self._add_pending(key, calls, records)
            raise
        for row in rows:
            self.learn(row["user_id"], row["month"], row["calls"], row["records"])

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
