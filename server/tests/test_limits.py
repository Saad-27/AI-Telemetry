"""Rate limit and quota logic without a database (ADR 0017). Flushes are in test_limits_db.py;
the 429 responses in test_ingest.py."""

import dataclasses
import uuid
from datetime import UTC, datetime
from typing import Any, NoReturn

import pytest

from app.core.entitlements import FREE, entitlements
from app.ingest.limits import Quota, RateLimiter

pytestmark = pytest.mark.anyio

KEY, OTHER = uuid.uuid4(), uuid.uuid4()
LIMITS = dataclasses.replace(FREE, records_per_s=2, records_burst=4, monthly_calls=100)


def at(year: int, month: int, day: int, hour: int = 0) -> float:
    return datetime(year, month, day, hour, tzinfo=UTC).timestamp()


class Down:
    """A pool whose database is unreachable."""

    def acquire(self, **_: Any) -> NoReturn:
        raise ConnectionRefusedError


def test_unknown_plans_get_the_free_limits() -> None:
    assert entitlements("free") is FREE
    assert entitlements("enterprise") is FREE


def test_bucket_refills_up_to_the_burst() -> None:
    now = [0.0]
    rate = RateLimiter(clock=lambda: now[0])
    assert [rate.wait_s(KEY, LIMITS) for _ in range(5)] == [0, 0, 0, 0, 0.5]
    now[0] = 100.0  # long idle: back to the burst, not 200 tokens
    assert [rate.wait_s(KEY, LIMITS) for _ in range(5)] == [0, 0, 0, 0, 0.5]
    assert rate.wait_s(OTHER, LIMITS) == 0  # buckets are per key


def test_debt_is_paid_off_at_the_rate() -> None:
    now = [0.0]
    rate = RateLimiter(clock=lambda: now[0])
    assert rate.wait_s(KEY, LIMITS) == 0
    rate.charge(KEY, 10)  # 4 - 1 - 10 = -7 tokens
    assert rate.wait_s(KEY, LIMITS) == 4.0  # (1 - -7) / 2
    now[0] = 4.0
    assert rate.wait_s(KEY, LIMITS) == 0


def test_buckets_are_bounded() -> None:
    rate = RateLimiter(clock=lambda: 0.0, max_entries=2)
    first, second, third = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for key in (first, second, first, third):  # `second` is least recently used
        rate.wait_s(key, LIMITS)
    rate.charge(first, 10)
    rate.charge(second, 10)  # evicted: a no-op
    assert rate.wait_s(first, LIMITS) > 0
    assert rate.wait_s(second, LIMITS) == 0


def test_quota_resets_at_the_start_of_the_utc_month() -> None:
    now = [at(2026, 12, 31, 23)]
    quota = Quota(Down(), clock=lambda: now[0])
    quota.add(KEY, 100)
    assert quota.wait_s(KEY, LIMITS) == 3600  # until 2027-01-01
    assert quota.wait_s(OTHER, LIMITS) == 0
    now[0] = at(2027, 1, 1)
    assert quota.wait_s(KEY, LIMITS) == 0


def test_a_higher_limit_unblocks_at_once() -> None:
    quota = Quota(Down(), clock=lambda: at(2026, 10, 17))
    quota.add(KEY, 100)
    assert quota.wait_s(KEY, LIMITS) > 0
    assert quota.wait_s(KEY, dataclasses.replace(LIMITS, monthly_calls=101)) == 0


async def test_failed_flush_keeps_the_counts() -> None:
    quota = Quota(Down(), clock=lambda: at(2026, 10, 17))
    await quota.flush()  # nothing pending: no connection attempted
    quota.add(KEY, 60)
    with pytest.raises(ConnectionRefusedError):
        await quota.flush()
    quota.add(KEY, 40)
    assert quota.wait_s(KEY, LIMITS) > 0  # 60 kept + 40 new


async def test_flush_errors_are_logged_by_type_only(caplog: pytest.LogCaptureFixture) -> None:
    quota = Quota(Down(), clock=lambda: at(2026, 10, 17))
    quota.add(KEY, 1)
    await quota.flush_logged()
    assert "quota: flush failed (ConnectionRefusedError)" in caplog.text
