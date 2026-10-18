"""When a machine builds totals, without a database (ADR 0023). Builds are in test_totals_db.py."""

from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any

import pytest
from prometheus_client import REGISTRY
from records import envelope, summary

import app.db.totals as totals
from app.db.totals import FIVE_MINUTES, Totals, late_buckets
from app.ingest.schema import Envelope, Summary, validate_records

NOON = datetime(2026, 10, 18, 12, tzinfo=UTC)
NOW = NOON + timedelta(seconds=90)
MINUTE = timedelta(minutes=1)


class Pool:
    def acquire(self, *, timeout: float) -> "Pool":
        return self

    async def __aenter__(self) -> "Pool":
        return self

    async def __aexit__(
        self, typ: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        return None


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def summaries(*minutes: datetime) -> list[Summary]:
    raw = [summary(minute=int(m.timestamp() * 1000)) for m in minutes]
    now_ms = int(NOW.timestamp() * 1000) + 3_600_000  # accepts minutes after NOW too
    return validate_records(Envelope.model_validate(envelope(raw)), now_ms).summaries


def builds(result: str) -> float:
    return REGISTRY.get_sample_value("gaslit_totals_builds_total", {"result": result}) or 0.0


def test_late_means_its_bucket_may_be_built() -> None:
    # The 11:55 bucket ended at 12:00: late from 12:02, built from 12:03.
    assert late_buckets([NOON - MINUTE], NOON + 2 * MINUTE - timedelta(seconds=1)) == []
    assert late_buckets([NOON - MINUTE, NOON - 4 * MINUTE], NOON + 2 * MINUTE) == [
        NOON - FIVE_MINUTES
    ]


@pytest.mark.anyio
async def test_builds_what_it_received_once_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[datetime] = []

    async def build(conn: Any, now: datetime) -> datetime:
        built.append(now)
        return totals.ready_through(now)

    monkeypatch.setattr(totals, "build", build)
    clock = Clock(NOW)
    machine = Totals(Pool(), clock=clock)
    assert not machine.due(NOW)  # received nothing: never wakes the database

    machine.note(summaries(NOON))  # in the 12:00 bucket
    # A machine that just started learns what is built with its first build.
    assert machine.due(NOW)
    await machine.build()
    assert not machine.due(NOW)  # the 12:00 bucket ends at 12:05, buildable from 12:08
    clock.now = NOW.replace(minute=8)
    assert machine.due(clock.now)
    await machine.build()
    assert built == [NOW, clock.now] and not machine.due(clock.now)
    clock.now += 10 * MINUTE  # nothing new received: nothing to build
    assert not machine.due(clock.now)


@pytest.mark.anyio
async def test_late_summaries_are_built_at_the_next_check(monkeypatch: pytest.MonkeyPatch) -> None:
    async def build(conn: Any, now: datetime) -> datetime:
        return totals.ready_through(now)

    monkeypatch.setattr(totals, "build", build)
    machine = Totals(Pool(), clock=Clock(NOW))
    machine.note(summaries(NOON - 30 * MINUTE))
    assert machine.due(NOW)
    await machine.build()
    assert not machine.due(NOW)


@pytest.mark.anyio
async def test_failed_or_locked_builds_are_tried_again(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    results: list[Any] = [ConnectionResetError(), None]

    async def build(conn: Any, now: datetime) -> datetime | None:
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result  # type: ignore[no-any-return]

    monkeypatch.setattr(totals, "build", build)
    machine = Totals(Pool(), clock=Clock(NOW))
    machine.note(summaries(NOON - 30 * MINUTE))
    failed, locked = builds("failed"), builds("locked")
    await machine.build()
    assert machine.due(NOW) and builds("failed") == failed + 1
    assert "totals: build failed (ConnectionResetError)" in caplog.text
    await machine.build()  # another machine was building
    assert machine.due(NOW) and builds("locked") == locked + 1
