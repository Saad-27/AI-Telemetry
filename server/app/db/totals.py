"""Pre-summed totals for the dashboard: per 5 minutes and per hour (ADR 0023).

The dashboard reads totals for the past and per-minute summaries only for the newest minutes,
so a read costs about the same for any range, whatever an account's traffic or number of app
processes. Totals hold every series column, so every dashboard filter still applies.

- Each 5-minute bucket is built BUILD_AFTER after it ends, for every project at once, by
  whichever machine is receiving summaries (`Totals`) or by the daily upkeep
  (app.db.partitions). An hour is built from its twelve buckets once they are all built.
- A summary that arrives after its bucket may have been built is late (an SDK sending a backlog
  after an outage). Ingest marks its project and bucket in `totals_stale`, and the next build
  rebuilds both from the summaries.
- `totals_watermark.built_through` is where built 5-minute totals end; hourly totals end at the
  hour it falls in. Reads use totals before it and summaries after it, so they are right
  whether a build has run or not, only slower.

Builds run as the schema owner, like partition upkeep: they read every project's summaries.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta

import asyncpg

from app.core.metrics import TOTALS_BUILD_SECONDS, TOTALS_BUILDS
from app.db import DB_TIMEOUT_S
from app.ingest.schema import DECODE_BUCKETS, ERROR_CLASSES, LATENCY_BUCKETS, Summary

FIVE_MINUTES = timedelta(minutes=5)
HOUR = timedelta(hours=1)
# Buckets and chart steps are counted from here, so they fall on round UTC times.
ORIGIN = datetime(2000, 1, 1, tzinfo=UTC)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ORIGIN_SQL = "'2000-01-01T00:00:00+00'"
# A bucket is built this long after it ends. The SDK sends a minute's summaries about 2 s after
# it ends, so nearly all have arrived.
BUILD_AFTER = timedelta(minutes=3)
# Ingest marks summaries late from this long after their bucket ended. It is a minute before
# any build, far more than ingest's database timeout, so a summary is in the build or marked.
LATE_AFTER = timedelta(minutes=2)
# Per-minute summaries and 5-minute totals are kept for today and this many UTC days before it:
# 48 to 72 hours. Late summaries are accepted for 24 hours (ADR 0016), so their bucket can
# always be rebuilt. Hourly totals are kept for the plan's retention.
DETAIL_DAYS = 2
CHECK_S = 60.0  # how often a machine that is receiving summaries checks for a bucket to build
BUILD_TIMEOUT_S = 60.0  # per statement; after a long pause a build covers many buckets
_LOCK = 20_023  # one machine at a time builds

log = logging.getLogger(__name__)

# The columns a total keeps: the series (brief §19.2), and the sums.
SERIES = (
    "provider", "route", "endpoint_host", "endpoint_region", "operation", "model", "streaming",
    "input_bucket", "service", "environment", "origin_region",
)  # fmt: skip
COUNTS = (
    "count", "ok", "cancelled", *(f"err_{c}" for c in ERROR_CLASSES),
    "input_tokens_sum", "output_tokens_sum", "output_tokens_estimated_count",
)  # fmt: skip
HISTOGRAMS = (
    ("ttft_hist", LATENCY_BUCKETS),
    ("duration_hist", LATENCY_BUCKETS),
    ("decode_hist", DECODE_BUCKETS),
)


def sums() -> str:
    """Every count summed, and every histogram summed bucket by bucket (ADR 0022).

    Postgres has no element-wise array sum; one `sum()` per bucket was the fastest way
    measured. `sum` of a bigint is numeric, hence the casts.
    """
    counts = [f"sum({c})::bigint AS {c}" for c in COUNTS]
    histograms = [
        "ARRAY[" + ", ".join(f"sum({h}[{i}])::bigint" for i in range(1, n + 1)) + f"] AS {h}"
        for h, n in HISTOGRAMS
    ]
    return ", ".join(counts + histograms)


def floor(t: datetime, step: timedelta) -> datetime:
    return t - (t - ORIGIN) % step


def ceil(t: datetime, step: timedelta) -> datetime:
    down = floor(t, step)
    return down if down == t else down + step


def ready_through(now: datetime) -> datetime:
    """Where the buckets that can be built at `now` end."""
    return floor(now - BUILD_AFTER, FIVE_MINUTES)


def late_buckets(minutes: Iterable[datetime], now: datetime) -> list[datetime]:
    """The buckets of these minutes that may already be built, so need rebuilding."""
    return sorted(
        {b for m in minutes if (b := floor(m, FIVE_MINUTES)) + FIVE_MINUTES <= now - LATE_AFTER}
    )


def _insert(table: str, bucket: str, source: str, where: str, project: str = "project_id") -> str:
    """Totals of `source` grouped by project, bucket and series. Built from constants only."""
    series = ", ".join(SERIES)
    columns = ", ".join([*COUNTS, *(h for h, _ in HISTOGRAMS)])
    groups = ", ".join(str(i) for i in range(1, len(SERIES) + 3))
    return (
        f"INSERT INTO {table} (project_id, bucket, {series}, {columns}) "  # noqa: S608
        f"SELECT {project}, {bucket}, {series}, {sums()} FROM {source} WHERE {where} "
        f"GROUP BY {groups}"
    )


# A range of buckets, for every project: $1 first bucket, $2 end.
_BUILD_5M = (
    "DELETE FROM totals_5m WHERE bucket >= $1 AND bucket < $2",
    _insert(
        "totals_5m",
        f"date_bin('5 minutes', minute, {_ORIGIN_SQL})",
        "summary_records",
        "minute >= $1 AND minute < $2",
    ),
)
_BUILD_1H = (
    "DELETE FROM totals_1h WHERE bucket >= $1 AND bucket < $2",
    _insert(
        "totals_1h",
        f"date_bin('1 hour', bucket, {_ORIGIN_SQL})",
        "totals_5m",
        "bucket >= $1 AND bucket < $2",
    ),
)
# Single buckets of single projects: $1 projects, $2 buckets, paired.
_PAIRS = "unnest($1::uuid[], $2::timestamptz[]) AS s(project_id, bucket)"
_REBUILD_5M = (
    f"DELETE FROM totals_5m t USING {_PAIRS} "  # noqa: S608
    "WHERE t.project_id = s.project_id AND t.bucket = s.bucket",
    _insert(
        "totals_5m",
        "s.bucket",
        f"{_PAIRS} JOIN summary_records r ON r.project_id = s.project_id "
        "AND r.minute >= s.bucket AND r.minute < s.bucket + interval '5 minutes'",
        "true",
        project="s.project_id",
    ),
)
_REBUILD_1H = (
    f"DELETE FROM totals_1h t USING {_PAIRS} "  # noqa: S608
    "WHERE t.project_id = s.project_id AND t.bucket = s.bucket",
    _insert(
        "totals_1h",
        "s.bucket",
        f"{_PAIRS} JOIN totals_5m f ON f.project_id = s.project_id "
        "AND f.bucket >= s.bucket AND f.bucket < s.bucket + interval '1 hour'",
        "true",
        project="s.project_id",
    ),
)


async def built_through(conn: asyncpg.Connection) -> datetime:
    return await conn.fetchval("SELECT built_through FROM totals_watermark")  # type: ignore[no-any-return]


async def build(
    conn: asyncpg.Connection, now: datetime, *, catch_up: bool = False
) -> datetime | None:
    """Builds every bucket ready at `now` and not built yet, then rebuilds the ones marked late.
    Returns where built totals end, or None if another machine is building.

    Totals partitions exist from yesterday on (app.db.partitions). Builds that start earlier,
    after more than a day with none, are left to the daily upkeep, which makes the partitions
    first and passes `catch_up`.
    """
    target = ready_through(now)
    async with conn.transaction():
        if not await conn.fetchval("SELECT pg_try_advisory_xact_lock($1)", _LOCK):
            return None
        built = await built_through(conn)
        if built < target:
            if not catch_up and built.date() < now.date() - timedelta(days=1):
                log.info("totals: built only to %s; waiting for the daily upkeep", built)
                return built
            await _run(conn, _BUILD_5M, built, target)
            first, last = floor(built, HOUR), floor(target, HOUR)
            if first < last:
                await _run(conn, _BUILD_1H, first, last)
            await conn.execute("UPDATE totals_watermark SET built_through = $1", target)
            built = target
        # Marks of buckets not built yet are dropped when the bucket is built.
        marks = await conn.fetch(
            "DELETE FROM totals_stale WHERE bucket < $1 RETURNING project_id, bucket", built
        )
        if marks:
            await _run(conn, _REBUILD_5M, [m[0] for m in marks], [m[1] for m in marks])
            hours = {(p, h) for p, b in marks if (h := floor(b, HOUR)) + HOUR <= built}
            if hours:
                await _run(conn, _REBUILD_1H, [p for p, _ in hours], [h for _, h in hours])
    return built


async def _run(conn: asyncpg.Connection, statements: Sequence[str], *args: object) -> None:
    for sql in statements:
        await conn.execute(sql, *args, timeout=BUILD_TIMEOUT_S)


def _now() -> datetime:
    return datetime.now(UTC)


def _minute(ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=ms)


class Totals:
    """Builds while this machine receives summaries, so it never wakes an idle database: the
    buckets this machine received are built within CHECK_S of being ready."""

    def __init__(
        self, pool: asyncpg.Pool, check_s: float = CHECK_S, clock: Callable[[], datetime] = _now
    ) -> None:
        self._pool = pool
        self._check_s = check_s
        self._clock = clock
        self._newest: datetime | None = None  # end of the newest bucket received here
        self._marked = False  # late summaries received here since the last build
        self._built: datetime | None = None

    def note(self, summaries: Sequence[Summary]) -> None:
        """Called by ingest once summaries are stored."""
        if not summaries:
            return
        end = floor(_minute(max(s.minute for s in summaries)), FIVE_MINUTES) + FIVE_MINUTES
        self._newest = end if self._newest is None else max(self._newest, end)
        if late_buckets([_minute(min(s.minute for s in summaries))], self._clock()):
            self._marked = True

    def due(self, now: datetime) -> bool:
        if self._marked:
            return True
        if self._newest is None:
            return False
        return self._built is None or self._built < min(self._newest, ready_through(now))

    async def build_every(self) -> None:
        while True:
            await asyncio.sleep(self._check_s)
            if self.due(self._clock()):
                await self.build()

    async def build(self) -> None:
        marked, self._marked = self._marked, False  # marks made while building wait for the next
        started = time.perf_counter()
        try:
            async with self._pool.acquire(timeout=DB_TIMEOUT_S) as conn:
                built = await build(conn, self._clock())
        except Exception as exc:
            self._marked |= marked
            TOTALS_BUILDS.labels("failed").inc()
            # Type only: driver messages can quote row values or connection details.
            log.warning("totals: build failed (%s)", type(exc).__name__)
            return
        finally:
            TOTALS_BUILD_SECONDS.observe(time.perf_counter() - started)
        if built is None:  # another machine is building; check again next time
            self._marked |= marked
            TOTALS_BUILDS.labels("locked").inc()
            return
        self._built = built
        TOTALS_BUILDS.labels("ok").inc()
