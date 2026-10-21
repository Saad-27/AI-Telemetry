"""The worker's hourly upkeep schedule, without a database (ADR 0020, ADR 0026)."""

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from prometheus_client import REGISTRY

import app.db.partitions as partitions
from app.core.entitlements import FREE, longest_retention_days
from app.core.metrics import PARTITIONS_MAINTAINED

NOON = datetime(2026, 10, 18, 12, tzinfo=UTC)


class Done(BaseException):
    pass


class Conn:
    async def close(self) -> None:
        return None


def connecting(refuse: int) -> Callable[[str], Awaitable[Conn]]:
    """A connect that refuses this many times, then hands out connections."""

    async def connect(url: str) -> Conn:
        nonlocal refuse
        if refuse:
            refuse -= 1
            raise ConnectionRefusedError
        return Conn()

    return connect


def clock(*times: datetime) -> Callable[[], datetime]:
    """These times, then Done, which stops the loop."""
    left = list(times)

    def now() -> datetime:
        if not left:
            raise Done
        return left.pop(0)

    return now


def test_retention_is_the_free_plans() -> None:
    assert longest_retention_days() == FREE.retention_days == 7


@pytest.mark.anyio
async def test_runs_every_time_and_retries_failures(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    runs: list[datetime] = []

    async def maintain(conn: Any, now: datetime, keep_days: int) -> bool:
        runs.append(now)
        return True

    hour = timedelta(hours=1)
    monkeypatch.setattr(partitions, "maintain", maintain)
    with pytest.raises(Done):
        await partitions.maintain_every(
            "url", 7, every_s=0, clock=clock(NOON, NOON + hour, NOON + 2 * hour),
            connect=connecting(refuse=1),
        )  # fmt: skip
    # The first run failed; the next ones ran, on the hour.
    assert runs == [NOON + hour, NOON + 2 * hour]
    assert "partitions: maintenance failed (ConnectionRefusedError)" in caplog.text


@pytest.mark.anyio
async def test_the_last_success_is_recorded_for_alerts(monkeypatch: pytest.MonkeyPatch) -> None:
    name = "gaslit_partitions_last_success_timestamp_seconds"
    result = [False]

    async def maintain(conn: Any, now: datetime, keep_days: int) -> bool:
        return result[0]

    async def run_once() -> None:
        with pytest.raises(Done):
            await partitions.maintain_every(
                "url", 7, every_s=0, clock=clock(NOON), connect=connecting(refuse=0)
            )

    monkeypatch.setattr(partitions, "maintain", maintain)
    PARTITIONS_MAINTAINED.set(0)
    await run_once()  # another run held the lock: not this one's success
    assert REGISTRY.get_sample_value(name) == 0
    result[0] = True
    await run_once()
    assert abs((REGISTRY.get_sample_value(name) or 0) - time.time()) < 60
