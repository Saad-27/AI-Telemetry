"""The daily partition upkeep schedule, without a database (ADR 0020)."""

import time
from collections.abc import Callable
from datetime import date, timedelta
from types import TracebackType
from typing import Any

import pytest
from prometheus_client import REGISTRY

import app.db.partitions as partitions
from app.core.entitlements import FREE, longest_retention_days
from app.core.metrics import PARTITIONS_MAINTAINED

DAY = date(2026, 10, 18)


class Done(Exception):
    pass


class Pool:
    def __init__(self) -> None:
        self.refuse = 1  # connections to refuse before handing one out

    def acquire(self, *, timeout: float) -> "Pool":
        return self

    async def __aenter__(self) -> "Pool":
        if self.refuse:
            self.refuse -= 1
            raise ConnectionRefusedError
        return self

    async def __aexit__(
        self, typ: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        return None


def test_retention_is_the_free_plans() -> None:
    assert longest_retention_days() == FREE.retention_days == 7


@pytest.mark.anyio
async def test_runs_once_a_day_and_retries_failures(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    runs: list[date] = []

    async def maintain(conn: Any, today: date, keep_days: int) -> bool:
        runs.append(today)
        return True

    checks = iter([DAY, DAY, DAY, DAY + timedelta(days=1), DAY + timedelta(days=1)])

    def today() -> date:
        try:
            return next(checks)
        except StopIteration:
            raise Done from None

    monkeypatch.setattr(partitions, "maintain", maintain)
    with pytest.raises(Done):
        await partitions.maintain_daily(Pool(), keep_days=7, check_s=0, today=today)
    # The first check failed and the second ran; then one run for the next day.
    assert runs == [DAY, DAY + timedelta(days=1)]
    assert "partitions: maintenance failed (ConnectionRefusedError)" in caplog.text


@pytest.mark.anyio
async def test_the_last_success_is_recorded_for_alerts(monkeypatch: pytest.MonkeyPatch) -> None:
    name = "gaslit_partitions_last_success_timestamp_seconds"
    result = [False]

    async def maintain(conn: Any, today: date, keep_days: int) -> bool:
        return result[0]

    def once() -> Callable[[], date]:
        days = [DAY]

        def today() -> date:
            if not days:
                raise Done
            return days.pop()

        return today

    monkeypatch.setattr(partitions, "maintain", maintain)
    PARTITIONS_MAINTAINED.set(0)
    pool = Pool()
    pool.refuse = 0
    with pytest.raises(Done):  # another machine held the lock: not this one's success
        await partitions.maintain_daily(pool, keep_days=7, check_s=0, today=once())
    assert REGISTRY.get_sample_value(name) == 0
    result[0] = True
    with pytest.raises(Done):
        await partitions.maintain_daily(pool, keep_days=7, check_s=0, today=once())
    assert abs((REGISTRY.get_sample_value(name) or 0) - time.time()) < 60
