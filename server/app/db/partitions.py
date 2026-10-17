"""Day partitions for the append-only data tables (brief §9.4, ADR 0013).

Run as the schema owner: `python -m app.db.partitions`. It creates the partitions for
yesterday through DAYS_AHEAD days from now, so a missed run is harmless for a week.
"""

import asyncio
from datetime import UTC, date, datetime, timedelta

import asyncpg

from app.core.config import load_settings

# Partitioned table -> its partition key column.
PARTITIONED = {"summary_records": "minute", "call_samples": "ts"}
DAYS_AHEAD = 7


def partition_name(table: str, day: date) -> str:
    return f"{table}_p{day:%Y%m%d}"


async def ensure_partitions(
    conn: asyncpg.Connection, today: date, days_ahead: int = DAYS_AHEAD
) -> None:
    for offset in range(-1, days_ahead + 1):
        day = today + timedelta(days=offset)
        # Names and bounds come from constants and dates, never from input. Bounds carry an
        # explicit UTC offset so the session time zone cannot shift them.
        for table in PARTITIONED:
            await conn.execute(
                f"CREATE TABLE IF NOT EXISTS {partition_name(table, day)} PARTITION OF {table} "
                f"FOR VALUES FROM ('{day}T00:00:00+00') "
                f"TO ('{day + timedelta(days=1)}T00:00:00+00')"
            )


async def _main() -> None:
    conn = await asyncpg.connect(load_settings().database_url)
    try:
        await ensure_partitions(conn, datetime.now(UTC).date())
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(_main())
