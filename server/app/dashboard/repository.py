"""Dashboard reads (brief §9.3, ADR 0022, ADR 0023).

The repository seam (brief §20.7): dashboard routes read metrics only through
`MetricsRepository`, so what sits behind it can change without them. A read sums the coarsest
pre-summed totals that fit the range (hourly, then 5-minute, app.db.totals) and per-minute
summaries for the rest: the edges, and the newest minutes not built yet. So it costs about the
same for any range, whatever an account's traffic. Histograms have fixed buckets, so summing
them bucket by bucket is exact over any range, group of series and number of app processes.
Percentiles come afterwards, from the sums (`quantile`).
"""

from collections.abc import Sequence
from dataclasses import astuple, dataclass, fields
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg

from app.core.metrics import DASHBOARD_QUERY_SECONDS
from app.db import DB_TIMEOUT_S
from app.db.totals import (
    COUNTS,
    DETAIL_DAYS,
    FIVE_MINUTES,
    HISTOGRAMS,
    HOUR,
    ceil,
    floor,
    sums,
)
from app.ingest.schema import ERROR_CLASSES

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


# The pieces of a range, in time order, each [from, to): summaries, 5-minute totals, hourly
# totals, 5-minute totals, summaries. Unused pieces are empty. $2 to $11 are their bounds.
_PIECES = (
    ("summary_records", "minute"),
    ("totals_5m", "bucket"),
    ("totals_1h", "bucket"),
    ("totals_5m", "bucket"),
    ("summary_records", "minute"),
)
_FILTERS = tuple(f.name for f in fields(Filters))  # columns, in the order astuple() gives
_FIRST_FILTER = 2 + 2 * len(_PIECES)
_STEP = f"${_FIRST_FILTER + len(_FILTERS)}"


def _piece(i: int, table: str, time: str) -> str:
    """One piece's rows, with the columns the reads group by. Built from constants only."""
    histograms = (f"{h}::bigint[] AS {h}" if time == "minute" else h for h, _ in HISTOGRAMS)
    filters = " AND ".join(
        f"(${n}::text IS NULL OR {column} = ${n})"
        for n, column in enumerate(_FILTERS, _FIRST_FILTER)
    )
    return (
        f"SELECT {time} AS t, provider, route, model, input_bucket, "  # noqa: S608
        f"{', '.join([*COUNTS, *histograms])} FROM {table} WHERE project_id = $1 "
        f"AND {time} >= ${2 + 2 * i} AND {time} < ${3 + 2 * i} AND {filters}"
    )


_ROWS = " UNION ALL ".join(_piece(i, *piece) for i, piece in enumerate(_PIECES))
_OVERVIEW = (
    f"SELECT provider, route, model, {sums()} FROM ({_ROWS}) r "  # noqa: S608
    "GROUP BY provider, route, model"
)
# Steps are counted from midnight UTC on 2000-01-01, like the totals' buckets.
_SERIES = (
    f"SELECT date_bin({_STEP}, t, '2000-01-01T00:00:00+00') AS start, input_bucket, "  # noqa: S608
    f"{sums()} FROM ({_ROWS}) r GROUP BY 1, 2 ORDER BY 1, 2"
)


def _bounds(
    start: datetime, end: datetime, built: datetime, grains: Sequence[timedelta]
) -> list[datetime]:
    """The pieces' bounds: the coarsest totals in `grains` that fit inside [start, built), and
    summaries for the rest."""
    upto = min(max(built, start), end)
    f0 = f1 = h0 = h1 = start
    a, b = ceil(start, FIVE_MINUTES), floor(upto, FIVE_MINUTES)
    if FIVE_MINUTES in grains and a < b:
        f0, f1 = a, b
    if HOUR in grains and (a := ceil(start, HOUR)) < (b := floor(upto, HOUR)):
        h0, h1 = a, b
    else:
        h0 = h1 = f1
    return [start, f0, f0, h0, h0, h1, h1, f1, f1, end]


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
        """Totals per provider, route and model, over the minutes in [start, end).

        Only hourly totals are kept past DETAIL_DAYS, so a `start` that old is moved back to
        the start of its hour.
        """
        rows = await self._read(
            "overview", _OVERVIEW, project_id, start, end, (FIVE_MINUTES, HOUR), filters
        )
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
        covers less than a step. Steps under an hour reach back DETAIL_DAYS at most.
        """
        grains = [g for g in (FIVE_MINUTES, HOUR) if step % g == timedelta(0)]
        rows = await self._read("series", _SERIES, project_id, start, end, grains, filters, step)
        return [Point(r["start"], r["input_bucket"], _sums_of(r)) for r in rows]

    async def _read(
        self,
        name: str,
        sql: str,
        project_id: UUID,
        start: datetime,
        end: datetime,
        grains: Sequence[timedelta],
        filters: Filters,
        *extra: Any,
    ) -> list[asyncpg.Record]:
        if start < datetime.now(UTC) - timedelta(days=DETAIL_DAYS):
            start = floor(start, HOUR)
        with DASHBOARD_QUERY_SECONDS.labels(name).time():
            async with (
                self._pool.acquire(timeout=DB_TIMEOUT_S) as conn,
                conn.transaction(readonly=True),
            ):
                # Row-level security shows dashboard_app this project's rows only (ADR 0013),
                # whatever the WHERE clause says. The WHERE clause is what uses the index.
                row = await conn.fetchrow(
                    "SELECT set_config('app.project_id', $1, true), "
                    "(SELECT built_through FROM totals_watermark)",
                    str(project_id),
                    timeout=DB_TIMEOUT_S,
                )
                bounds = _bounds(start, end, row[1], grains)
                return await conn.fetch(  # type: ignore[no-any-return]
                    sql, project_id, *bounds, *astuple(filters), *extra, timeout=DB_TIMEOUT_S
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
