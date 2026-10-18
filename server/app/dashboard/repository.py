"""Dashboard reads: the per-minute summaries summed when a page asks (brief §9.3, ADR 0008,
ADR 0022).

The repository seam (brief §20.7): dashboard routes read metrics only through
`MetricsRepository`, so what sits behind it (pre-summed hours, another database) can change
without them. Histograms have fixed buckets, so summing them bucket by bucket is exact over any
range, any group of series and any number of app processes. Percentiles come afterwards, from
the sums (`quantile`).
"""

from collections.abc import Sequence
from dataclasses import astuple, dataclass, fields
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg

from app.core.metrics import DASHBOARD_QUERY_SECONDS
from app.db import DB_TIMEOUT_S
from app.ingest.schema import DECODE_BUCKETS, ERROR_CLASSES, LATENCY_BUCKETS

MIN_SAMPLES = 20  # no percentile from fewer values (brief §10.3)


@dataclass(frozen=True, slots=True)
class Filters:
    """Narrows a read to the summaries that match. None matches anything."""

    provider: str | None = None
    route: str | None = None
    model: str | None = None
    service: str | None = None
    environment: str | None = None
    origin_region: str | None = None


ANY = Filters()


@dataclass(frozen=True, slots=True)
class Sums:
    """Totals over a range, named as in the summary record (brief §19.2)."""

    count: int
    ok: int
    cancelled: int
    errors: dict[str, int]  # by error class; only the classes that occurred, as on the wire
    ttft_hist: tuple[int, ...]
    duration_hist: tuple[int, ...]
    decode_hist: tuple[int, ...]
    input_tokens_sum: int
    output_tokens_sum: int
    output_tokens_estimated_count: int


@dataclass(frozen=True, slots=True)
class ModelSums:
    """An overview row: one model on one route."""

    provider: str
    route: str
    model: str
    sums: Sums


@dataclass(frozen=True, slots=True)
class Point:
    """A chart point: one step from `start`, for one input bucket."""

    start: datetime
    input_bucket: str
    sums: Sums


_COUNTS = (
    "count", "ok", "cancelled", *(f"err_{c}" for c in ERROR_CLASSES),
    "input_tokens_sum", "output_tokens_sum", "output_tokens_estimated_count",
)  # fmt: skip
_HISTOGRAMS = (
    ("ttft_hist", LATENCY_BUCKETS),
    ("duration_hist", LATENCY_BUCKETS),
    ("decode_hist", DECODE_BUCKETS),
)
_FILTERS = tuple(f.name for f in fields(Filters))  # columns, in the order astuple() gives


def _sums() -> str:
    """Every count summed, and every histogram summed bucket by bucket.

    Postgres has no element-wise array sum. One `sum()` per bucket was the fastest way measured:
    5 times faster than `unnest ... WITH ORDINALITY`, 20 times faster than an aggregate written
    in SQL (ADR 0022). `sum` of a bigint is numeric, hence the casts.
    """
    counts = [f"sum({c})::bigint AS {c}" for c in _COUNTS]
    histograms = [
        "ARRAY[" + ", ".join(f"sum({h}[{i}])" for i in range(1, n + 1)) + f"] AS {h}"
        for h, n in _HISTOGRAMS
    ]
    return ", ".join(counts + histograms)


# $1 project, $2 start, $3 end, then one parameter per filter. Column names come from the
# constants above only.
_WHERE = "project_id = $1 AND minute >= $2 AND minute < $3 AND " + " AND ".join(
    f"(${i}::text IS NULL OR {column} = ${i})" for i, column in enumerate(_FILTERS, 4)
)
_OVERVIEW = (
    f"SELECT provider, route, model, {_sums()} FROM summary_records "  # noqa: S608
    f"WHERE {_WHERE} GROUP BY provider, route, model"
)
# Steps are counted from midnight UTC on 2000-01-01, so 1 minute, 5 minutes and 1 hour fall on
# round times.
_SERIES = (
    f"SELECT date_bin(${len(_FILTERS) + 4}, minute, '2000-01-01T00:00:00+00') AS start, "  # noqa: S608
    f"input_bucket, {_sums()} FROM summary_records WHERE {_WHERE} GROUP BY 1, 2 ORDER BY 1, 2"
)


def _sums_of(row: asyncpg.Record) -> Sums:
    return Sums(
        count=row["count"],
        ok=row["ok"],
        cancelled=row["cancelled"],
        errors={c: n for c in ERROR_CLASSES if (n := row[f"err_{c}"])},
        ttft_hist=tuple(row["ttft_hist"]),
        duration_hist=tuple(row["duration_hist"]),
        decode_hist=tuple(row["decode_hist"]),
        input_tokens_sum=row["input_tokens_sum"],
        output_tokens_sum=row["output_tokens_sum"],
        output_tokens_estimated_count=row["output_tokens_estimated_count"],
    )


class MetricsRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def overview(
        self, project_id: UUID, start: datetime, end: datetime, filters: Filters = ANY
    ) -> list[ModelSums]:
        """Totals per provider, route and model, over the minutes in [start, end)."""
        rows = await self._read("overview", _OVERVIEW, project_id, start, end, filters)
        return [ModelSums(r["provider"], r["route"], r["model"], _sums_of(r)) for r in rows]

    async def series(
        self,
        project_id: UUID,
        start: datetime,
        end: datetime,
        step: timedelta,
        filters: Filters = ANY,
    ) -> list[Point]:
        """Totals per step and input bucket, over the minutes in [start, end), oldest first.

        Steps with no calls are left out. `start` should fall on a step, or the first point
        covers less than a step.
        """
        rows = await self._read("series", _SERIES, project_id, start, end, filters, step)
        return [Point(r["start"], r["input_bucket"], _sums_of(r)) for r in rows]

    async def _read(
        self,
        name: str,
        sql: str,
        project_id: UUID,
        start: datetime,
        end: datetime,
        filters: Filters,
        *extra: Any,
    ) -> list[asyncpg.Record]:
        with DASHBOARD_QUERY_SECONDS.labels(name).time():
            async with (
                self._pool.acquire(timeout=DB_TIMEOUT_S) as conn,
                conn.transaction(readonly=True),
            ):
                # Row-level security shows dashboard_app this project's rows only (ADR 0013),
                # whatever the WHERE clause says. The WHERE clause is what uses the index.
                await conn.execute(
                    "SELECT set_config('app.project_id', $1, true)",
                    str(project_id),
                    timeout=DB_TIMEOUT_S,
                )
                return await conn.fetch(  # type: ignore[no-any-return]
                    sql, project_id, start, end, *astuple(filters), *extra, timeout=DB_TIMEOUT_S
                )


def quantile(hist: Sequence[int], bounds: Sequence[float], q: float) -> float | None:
    """The q-quantile of a summed histogram, or None from under MIN_SAMPLES values (§10.3).

    Interpolated linearly inside the bucket it falls in, so it is off by at most that bucket's
    width (brief §9.4). Buckets hold values up to and including their bound (ADR 0003); the
    first starts at 0. In the +inf bucket the answer is the last bound, meaning "at least
    this", as Prometheus reports it.
    """
    total = sum(hist)
    if total < MIN_SAMPLES:
        return None
    rank = q * total
    below = 0
    for i, n in enumerate(hist):
        if n and below + n >= rank:
            if i == len(bounds):
                break
            lower = bounds[i - 1] if i else 0
            return lower + (bounds[i] - lower) * (rank - below) / n
        below += n
    return float(bounds[-1])
