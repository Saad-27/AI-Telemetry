"""Rate limit and quota logic without a database (ADR 0017, ADR 0019). Flushes are in
test_limits_db.py; the 429 responses in test_ingest.py."""

import dataclasses
import uuid
from datetime import UTC, date, datetime
from typing import Any, NoReturn

import pytest
from prometheus_client import REGISTRY

from app.core.entitlements import FREE, entitlements
from app.ingest.keys import IngestKey
from app.ingest.limits import Quota, RateLimiter

pytestmark = pytest.mark.anyio

KEY, OTHER = uuid.uuid4(), uuid.uuid4()
LIMITS = dataclasses.replace(
    FREE, records_per_s=2, records_burst=4, monthly_calls=100, monthly_records=150
)


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
    quota.add(KEY, 100, 1)
    assert quota.wait_s(KEY, LIMITS) == 3600  # until 2027-01-01
    assert quota.wait_s(OTHER, LIMITS) == 0
    now[0] = at(2027, 1, 1)
    assert quota.wait_s(KEY, LIMITS) == 0


def test_a_higher_limit_unblocks_at_once() -> None:
    quota = Quota(Down(), clock=lambda: at(2026, 10, 17))
    quota.add(KEY, 100, 1)
    assert quota.wait_s(KEY, LIMITS) > 0
    assert quota.wait_s(KEY, dataclasses.replace(LIMITS, monthly_calls=101)) == 0


def test_records_have_their_own_cap() -> None:
    # A flood of samples counts no calls, but fills the database.
    quota = Quota(Down(), clock=lambda: at(2026, 10, 17))
    quota.add(KEY, 0, 149)
    assert quota.wait_s(KEY, LIMITS) == 0
    quota.add(KEY, 0, 1)
    assert quota.wait_s(KEY, LIMITS) > 0


def test_learned_totals_block_at_once() -> None:
    # A machine that has just started learns them from the key lookup.
    quota = Quota(Down(), clock=lambda: at(2026, 10, 17))
    quota.learn(KEY, date(2026, 9, 1), 100, 0)  # last month's
    assert quota.wait_s(KEY, LIMITS) == 0
    quota.learn(KEY, date(2026, 10, 1), 99, 0)
    quota.add(KEY, 1, 1)
    assert quota.wait_s(KEY, LIMITS) > 0


def test_an_older_answer_never_lowers_the_totals() -> None:
    # A key lookup and a flush can return out of order.
    quota = Quota(Down(), clock=lambda: at(2026, 10, 17))
    quota.learn(KEY, date(2026, 10, 1), 100, 3)
    quota.learn(KEY, date(2026, 10, 1), 90, 5)
    quota.learn(KEY, date(2026, 9, 1), 0, 0)
    assert quota.wait_s(KEY, LIMITS) > 0
    assert quota.wait_s(KEY, dataclasses.replace(LIMITS, monthly_calls=101)) == 0


def test_blocks_reads_the_keys_plan_and_unblock_forgets() -> None:
    quota = Quota(Down(), clock=lambda: at(2026, 10, 17))
    key = IngestKey(uuid.uuid4(), uuid.uuid4(), KEY, "free")
    quota.learn(KEY, date(2026, 10, 1), FREE.monthly_calls - 1, 0)
    assert not quota.blocks(key)
    quota.learn(KEY, date(2026, 10, 1), FREE.monthly_calls, 0)
    assert quota.blocks(key)
    assert quota.blocks(dataclasses.replace(key, plan="unknown"))  # the free plan's limits
    quota.unblock(KEY)
    assert not quota.blocks(key)  # until the key lookup returns the totals again


async def test_failed_flush_keeps_the_counts() -> None:
    def failures() -> float:
        return REGISTRY.get_sample_value("gaslit_quota_flushes_total", {"result": "failed"}) or 0

    before = failures()
    quota = Quota(Down(), clock=lambda: at(2026, 10, 17))
    await quota.flush()  # nothing pending: no connection attempted
    quota.add(KEY, 60, 100)
    with pytest.raises(ConnectionRefusedError):
        await quota.flush()
    assert failures() - before == 1
    quota.add(KEY, 39, 49)
    assert quota.wait_s(KEY, LIMITS) == 0  # 99 calls, 149 records
    quota.add(KEY, 0, 1)
    assert quota.wait_s(KEY, LIMITS) > 0


async def test_flush_errors_are_logged_by_type_only(caplog: pytest.LogCaptureFixture) -> None:
    quota = Quota(Down(), clock=lambda: at(2026, 10, 17))
    quota.add(KEY, 1, 1)
    await quota.flush_logged()
    assert "quota: flush failed (ConnectionRefusedError)" in caplog.text
