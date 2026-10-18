"""Day partitions for the data tables, and retention (brief §9.4, ADR 0013, ADR 0020, ADR 0023).

`maintain` creates the partitions for yesterday through DAYS_AHEAD days from now, so a missed
run is harmless for a week, builds the dashboard's totals, and drops the days past retention.
The app runs it at startup and once each UTC day (`maintain_daily`). By hand, as the schema
owner: `python -m app.db.partitions` (`make migrate` does).
"""

import asyncio
import logging
import re
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta

import asyncpg

from app.core.config import load_settings
from app.core.entitlements import longest_retention_days
from app.core.metrics import PARTITIONS_MAINTAINED
from app.db import DB_TIMEOUT_S
from app.db.totals import DETAIL_DAYS, build, built_through

log = logging.getLogger(__name__)

# Partitioned table -> its partition key column.
PARTITIONED = {
    "summary_records": "minute",
    "call_samples": "ts",
    "totals_5m": "bucket",
    "totals_1h": "bucket",
}
DAYS_AHEAD = 7
CHECK_S = 3600.0  # how often the app checks whether a new UTC day has begun
# Creating or dropping a partition locks its parent table, which queues inserts behind it.
# Giving up after this keeps ingest's wait well under its own query timeout; the next day's
# run tries again.
LOCK_TIMEOUT = "1s"
_ADVISORY_LOCK = 20_020  # any fixed number: one machine at a time runs the DDL


def partition_name(table: str, day: date) -> str:
    return f"{table}_p{day:%Y%m%d}"


def _partition_day(table: str, name: str) -> date | None:
    match = re.fullmatch(rf"{table}_p(\d{{8}})", name)
    try:
        return datetime.strptime(match[1], "%Y%m%d").date() if match else None
    except ValueError:  # not a real date, so not one of ours
        return None


async def ensure_partitions(
    conn: asyncpg.Connection, today: date, days_ahead: int = DAYS_AHEAD
) -> None:
    for offset in range(-1, days_ahead + 1):
        day = today + timedelta(days=offset)
        # Names and bounds come from constants and dates, never from input. Bounds carry an
        # explicit UTC offset so the session time zone cannot shift them. IF NOT EXISTS misses
        # a partition another session makes at the same moment, which is just as good.
        for table in PARTITIONED:
            with suppress(asyncpg.DuplicateTableError, asyncpg.UniqueViolationError):
                await conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {partition_name(table, day)} "
                    f"PARTITION OF {table} FOR VALUES FROM ('{day}T00:00:00+00') "
                    f"TO ('{day + timedelta(days=1)}T00:00:00+00')"
                )


async def drop_expired(conn: asyncpg.Connection, before: Mapping[str, date]) -> list[str]:
    """Drops each table's partitions of the days before `before[table]` and returns their names.
    Partitions named otherwise are left alone."""
    dropped = []
    for table, cutoff in before.items():
        rows = await conn.fetch(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "WHERE i.inhparent = $1::regclass",
            table,
        )
        days = sorted(d for r in rows if (d := _partition_day(table, r["relname"])) is not None)
        for day in days:
            if day >= cutoff:
                break
            name = partition_name(table, day)  # rebuilt from the date, never from the catalog
            await conn.execute(f"DROP TABLE {name}")
            dropped.append(name)
    return dropped


def retention(today: date, keep_days: int, built: datetime) -> dict[str, date]:
    """The first day kept, per table. On day D, days before D - keep are dropped, so rows live
    between keep and keep + 1 days: `keep_days` (the longest plan's) for samples and hourly
    totals, DETAIL_DAYS for per-minute summaries and 5-minute totals (ADR 0023). Summaries not
    yet in totals are never dropped."""
    detail = today - timedelta(days=DETAIL_DAYS)
    kept = today - timedelta(days=keep_days)
    return {
        "summary_records": min(detail, built.date()),
        "totals_5m": detail,
        "totals_1h": kept,
        "call_samples": kept,
    }


async def maintain(conn: asyncpg.Connection, now: datetime, keep_days: int) -> bool:
    """Creates the coming days' partitions, builds totals and drops expired days. False if
    another connection holds the job's advisory lock, that is another machine is at it."""
    if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", _ADVISORY_LOCK):
        return False
    try:
        await conn.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
        today = now.date()
        await ensure_partitions(conn, today)
        # After more than a day without a build (every machine stopped), the days still to
        # build need totals partitions too.
        built = await built_through(conn)
        if (gap := (today - built.date()).days) > 1:
            await ensure_partitions(conn, built.date() + timedelta(days=1), days_ahead=gap - 1)
        built = await build(conn, now, catch_up=True) or built  # None: another machine is
        dropped = await drop_expired(conn, retention(today, keep_days, built))
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", _ADVISORY_LOCK)
    log.info("partitions: maintained for %s, dropped %s", today, ", ".join(dropped) or "none")
    return True


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def maintain_daily(
    pool: asyncpg.Pool,
    keep_days: int,
    check_s: float = CHECK_S,
    clock: Callable[[], datetime] = _utc_now,
) -> None:
    """Runs `maintain` now, then whenever a new UTC day has begun. At most one run a day, so
    an idle database is woken at most once a day. A failed run is retried at the next check."""
    done: date | None = None
    while True:
        now = clock()
        if (day := now.date()) != done:
            try:
                async with pool.acquire(timeout=DB_TIMEOUT_S) as conn:
                    if await maintain(conn, now, keep_days):
                        PARTITIONS_MAINTAINED.set_to_current_time()
                done = day
            except Exception as exc:
                # Type only: driver messages can quote connection details.
                log.warning("partitions: maintenance failed (%s)", type(exc).__name__)
        await asyncio.sleep(check_s)


async def _main() -> None:
    conn = await asyncpg.connect(load_settings().database_url)
    try:
        await maintain(conn, _utc_now(), longest_retention_days())
    finally:
        await conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(_main())
