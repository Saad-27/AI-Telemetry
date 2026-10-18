"""Dashboard reads: sums, ranges, steps, filters and tenants (ADR 0022), from summaries and from
totals (ADR 0023), and percentiles."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest
from prometheus_client import REGISTRY
from records import envelope, now_ms, summary

from app.dashboard.repository import (
    Filters,
    MetricsRepository,
    ModelSums,
    Sums,
    _bounds,
    quantile,
)
from app.db.partitions import ensure_partitions
from app.db.totals import FIVE_MINUTES, build
from app.db.totals import HOUR as ONE_HOUR
from app.ingest.schema import (
    DECODE_BOUNDS_TPS,
    DECODE_BUCKETS,
    LATENCY_BOUNDS_MS,
    LATENCY_BUCKETS,
    Envelope,
    validate_records,
)
from app.ingest.sink import PostgresSink

# An hour boundary in the last day, so 5-minute and 1-hour steps start on it.
HOUR_MS = (now_ms() - 2 * 3_600_000) // 3_600_000 * 3_600_000
HOUR = datetime.fromtimestamp(HOUR_MS / 1000, UTC)
MINUTE = timedelta(minutes=1)


def hist(n: int, size: int = LATENCY_BUCKETS, at: int = 3) -> list[int]:
    return [n if i == at else 0 for i in range(size)]


def calls(minute: int, n: int, **series: Any) -> dict[str, Any]:
    """A summary of `n` successful calls `minute` minutes after HOUR."""
    record = summary(
        minute=HOUR_MS + minute * 60_000,
        count=n,
        ok=n,
        cancelled=0,
        errors={},
        ttft_hist=hist(n),
        duration_hist=hist(n, at=7),
        decode_hist=hist(n, DECODE_BUCKETS),
        input_tokens_sum=1000 * n,
        output_tokens_sum=n,
        output_tokens_estimated_count=0,
    )
    record["series"].update(series)
    return record


def sums(n: int, errors: dict[str, int] | None = None) -> Sums:
    """What `calls` adds up to, `n` successful calls in all, plus `errors`."""
    errored = sum((errors or {}).values())
    return Sums(
        count=n + errored,
        ok=n,
        cancelled=0,
        errors=errors or {},
        ttft_hist=tuple(hist(n)),
        duration_hist=tuple(hist(n, at=7)),
        decode_hist=tuple(hist(n, DECODE_BUCKETS)),
        input_tokens_sum=1000 * n,
        output_tokens_sum=n,
        output_tokens_estimated_count=0,
    )


async def store(pool: asyncpg.Pool, project: uuid.UUID, *summaries: dict[str, Any]) -> None:
    records = validate_records(Envelope.model_validate(envelope(list(summaries))), now_ms())
    assert not records.errors, records.errors
    await PostgresSink(pool).write(project, records.summaries, [])


# Percentiles


def test_quantile_interpolates_inside_the_bucket() -> None:
    # 40 values in (50, 100]: the median is halfway through the bucket.
    assert quantile(hist(40, at=2), LATENCY_BOUNDS_MS, 0.5) == 75
    assert quantile(hist(40, at=2), LATENCY_BOUNDS_MS, 1.0) == 100  # bounds are inclusive
    assert quantile(hist(40, at=0), LATENCY_BOUNDS_MS, 0.5) == 12.5  # the first starts at 0


def test_quantile_across_buckets() -> None:
    h = [0] * LATENCY_BUCKETS
    h[4], h[5] = 30, 10  # 30 in (200, 300], 10 in (300, 500]
    assert quantile(h, LATENCY_BOUNDS_MS, 0.5) == pytest.approx(266.67, abs=0.01)
    assert quantile(h, LATENCY_BOUNDS_MS, 0.95) == 460  # rank 38: 8 of the 10 above 300


def test_quantile_in_the_last_bucket_is_at_least_the_last_bound() -> None:
    assert quantile(hist(25, at=LATENCY_BUCKETS - 1), LATENCY_BOUNDS_MS, 0.5) == 120_000
    assert quantile(hist(25, DECODE_BUCKETS, at=DECODE_BUCKETS - 1), DECODE_BOUNDS_TPS, 0.5) == (
        1000
    )


def test_no_quantile_from_under_20_values() -> None:
    assert quantile(hist(19), LATENCY_BOUNDS_MS, 0.5) is None
    assert quantile(hist(20), LATENCY_BOUNDS_MS, 0.5) is not None
    assert quantile([0] * LATENCY_BUCKETS, LATENCY_BOUNDS_MS, 0.5) is None


# Which pieces a range is read from


def test_bounds_use_the_coarsest_totals_that_fit() -> None:
    t = HOUR + 2 * MINUTE  # an edge inside a 5-minute bucket
    both = (FIVE_MINUTES, ONE_HOUR)
    end = t + 3 * ONE_HOUR

    def pieces(built: datetime, grains: tuple[timedelta, ...] = both) -> list[timedelta]:
        """Each piece's length: summaries, 5-minute, hourly, 5-minute, summaries."""
        b = _bounds(t, end, built, grains)
        return [b[i + 1] - b[i] for i in range(0, 10, 2)]

    m = MINUTE
    # Totals built through the end: summaries only at the edges.
    assert pieces(end) == [3 * m, 55 * m, 2 * ONE_HOUR, 0 * m, 2 * m]
    # Built through 45 minutes past the second hour: summaries after that.
    assert pieces(HOUR + 2 * ONE_HOUR + 45 * m) == [3 * m, 55 * m, ONE_HOUR, 45 * m, 17 * m]
    assert pieces(HOUR) == [0 * m, 0 * m, 0 * m, 0 * m, 3 * ONE_HOUR]  # nothing built yet
    assert pieces(end, (FIVE_MINUTES,)) == [3 * m, 175 * m, 0 * m, 0 * m, 2 * m]
    assert pieces(end, ()) == [0 * m, 0 * m, 0 * m, 0 * m, 3 * ONE_HOUR]  # 1-minute steps


# Reads from Postgres, as dashboard_app


@pytest.fixture
async def owner_pool(database_url: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(database_url, min_size=0, max_size=1)
    async with pool.acquire() as conn:
        await ensure_partitions(conn, datetime.now(UTC).date())
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture(params=[False, True], ids=["from-summaries", "from-totals"])
def built(request: pytest.FixtureRequest) -> bool:
    return bool(request.param)


async def settle(pool: asyncpg.Pool, built: bool) -> None:
    """Totals built from an hour before HOUR to now, or none from then, so reads take the
    tests' data from totals or from summaries. Either way the answers are the same."""
    async with pool.acquire() as conn:
        await conn.execute("UPDATE totals_watermark SET built_through = $1", HOUR - ONE_HOUR)
        if built:
            assert await build(conn, datetime.now(UTC)) is not None


@pytest.fixture
async def repo(database_url: str) -> AsyncIterator[MetricsRepository]:
    """Connected as the role the dashboard will use, under row-level security (ADR 0013)."""

    async def as_dashboard_app(conn: asyncpg.Connection) -> None:
        await conn.execute("SET ROLE dashboard_app")

    pool = await asyncpg.create_pool(database_url, min_size=0, max_size=1, setup=as_dashboard_app)
    try:
        yield MetricsRepository(pool)
    finally:
        await pool.close()


@pytest.mark.db
@pytest.mark.anyio
async def test_overview_sums_every_process_and_minute(
    owner_pool: asyncpg.Pool, repo: MetricsRepository, built: bool
) -> None:
    project = uuid.uuid4()
    gpt = {"provider": "openai", "endpoint_host": "api.openai.com", "model": "gpt-x"}
    await store(
        owner_pool,
        project,
        calls(0, 5),
        calls(0, 7),  # a second process, same minute and series
        calls(1, 1, input_bucket="xs"),  # same model, another input bucket
        {**calls(1, 2, **gpt), "count": 5, "errors": {"overloaded": 2, "auth_error": 1}},
        calls(60, 100),  # at the end of the range: not included
    )
    await store(owner_pool, uuid.uuid4(), calls(0, 1000))  # another project
    await settle(owner_pool, built)
    rows = await repo.overview(project, HOUR, HOUR + 60 * MINUTE)
    assert sorted(rows, key=lambda r: r.model) == [
        # Defaults from records.summary(): anthropic, direct, claude-sonnet-x-20260801.
        ModelSums("anthropic", "direct", "claude-sonnet-x-20260801", sums(13)),
        ModelSums("openai", "direct", "gpt-x", sums(2, {"overloaded": 2, "auth_error": 1})),
    ]
    assert type(rows[0].sums.input_tokens_sum) is int  # not Decimal: sum(bigint) is numeric
    assert await repo.overview(project, HOUR + 2 * MINUTE, HOUR + 60 * MINUTE) == []


@pytest.mark.db
@pytest.mark.anyio
async def test_series_steps_and_input_buckets(
    owner_pool: asyncpg.Pool, repo: MetricsRepository, built: bool
) -> None:
    project = uuid.uuid4()
    await store(
        owner_pool,
        project,
        calls(3, 1),
        calls(4, 2),
        calls(4, 4, input_bucket="l"),
        calls(5, 8),
        calls(59, 16),
    )
    await settle(owner_pool, built)
    points = await repo.series(project, HOUR, HOUR + 60 * MINUTE, 5 * MINUTE)
    assert [(p.start - HOUR, p.input_bucket, p.sums.count) for p in points] == [
        (0 * MINUTE, "l", 4),
        (0 * MINUTE, "s", 3),  # minutes 3 and 4
        (5 * MINUTE, "s", 8),
        (55 * MINUTE, "s", 16),
    ]
    hourly = await repo.series(project, HOUR, HOUR + 60 * MINUTE, 60 * MINUTE)
    assert [(p.start, p.input_bucket, p.sums.count) for p in hourly] == [
        (HOUR, "l", 4),
        (HOUR, "s", 27),
    ]


@pytest.mark.db
@pytest.mark.anyio
async def test_filters(owner_pool: asyncpg.Pool, repo: MetricsRepository, built: bool) -> None:
    project = uuid.uuid4()
    await store(
        owner_pool,
        project,
        calls(0, 1),  # summariser, prod, fra
        calls(0, 2, service="chat"),
        calls(0, 4, environment="dev"),
        calls(0, 8, origin_region=None),
        calls(0, 16, route="bedrock", endpoint_host=None, endpoint_region="eu-central-1"),
        calls(0, 32, model="claude-haiku-x"),
    )
    await settle(owner_pool, built)

    async def total(**filters: str) -> int:
        rows = await repo.overview(project, HOUR, HOUR + ONE_HOUR, Filters(**filters))
        return sum(r.sums.count for r in rows)

    assert await total() == 63
    assert await total(service="chat") == 2
    assert await total(service="summariser", environment="prod") == 57
    assert await total(origin_region="fra") == 55
    assert await total(route="bedrock") == 16
    assert await total(provider="openai") == 0
    assert await total(model="claude-haiku-x", route="direct") == 32
    points = await repo.series(
        project, HOUR, HOUR + MINUTE, MINUTE, Filters(model="claude-haiku-x")
    )
    assert [p.sums.count for p in points] == [32]


@pytest.mark.db
@pytest.mark.anyio
async def test_reads_are_timed(repo: MetricsRepository) -> None:
    def runs(query: str) -> float:
        name = "gaslit_dashboard_query_seconds_count"
        return REGISTRY.get_sample_value(name, {"query": query}) or 0.0

    before = runs("overview"), runs("series")
    await repo.overview(uuid.uuid4(), HOUR, HOUR + MINUTE)
    await repo.series(uuid.uuid4(), HOUR, HOUR + MINUTE, MINUTE)
    assert (runs("overview"), runs("series")) == (before[0] + 1, before[1] + 1)
