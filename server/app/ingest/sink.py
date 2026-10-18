"""Where accepted records go: the Sink seam (brief §20.7). Postgres now, a stream at stage 2."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

import asyncpg

from app.db import DB_TIMEOUT_S
from app.db.totals import late_buckets
from app.ingest.schema import ERROR_CLASSES, Sample, Summary

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

Columns = tuple[tuple[str, str], ...]  # (column, Postgres type)

_SUMMARY_COLUMNS: Columns = (
    ("minute", "timestamptz"),
    ("summary_id", "uuid"),
    ("provider", "text"),
    ("route", "text"),
    ("endpoint_host", "text"),
    ("endpoint_region", "text"),
    ("operation", "text"),
    ("model", "text"),
    ("streaming", "boolean"),
    ("input_bucket", "text"),
    ("service", "text"),
    ("environment", "text"),
    ("origin_region", "text"),
    ("count", "integer"),
    ("ok", "integer"),
    ("cancelled", "integer"),
    *((f"err_{c}", "integer") for c in ERROR_CLASSES),
    ("ttft_hist", "integer[]"),
    ("duration_hist", "integer[]"),
    ("decode_hist", "integer[]"),
    ("input_tokens_sum", "bigint"),
    ("output_tokens_sum", "bigint"),
    ("output_tokens_estimated_count", "integer"),
)

_SAMPLE_COLUMNS: Columns = (
    ("ts", "timestamptz"),
    ("event_id", "uuid"),
    ("reason", "text"),
    ("duration_ms", "real"),
    ("ttft_ms", "real"),
    ("provider", "text"),
    ("route", "text"),
    ("endpoint_host", "text"),
    ("endpoint_region", "text"),
    ("operation", "text"),
    ("model_requested", "text"),
    ("model_returned", "text"),
    ("streaming", "boolean"),
    ("has_tools", "boolean"),
    ("input_tokens", "integer"),
    ("output_tokens", "integer"),
    ("cached_input_tokens", "integer"),
    ("output_tokens_estimated", "boolean"),
    ("input_bucket", "text"),
    ("status", "text"),
    ("error_class", "text"),
    ("http_status", "smallint"),
    ("provider_error_code", "text"),
    ("service", "text"),
    ("environment", "text"),
    ("label", "text"),
    ("origin_region", "text"),
    ("provider_sdk", "text"),
    ("provider_sdk_version", "text"),
)


def _insert(table: str, columns: Columns) -> str:
    """One `INSERT ... SELECT FROM unnest()` per batch, one array per column (ADR 0013).

    Built from the constants above only. Histograms travel as text ('{1,2,3}') because unnest
    flattens an array of arrays, and are cast back per row.
    """
    names = ", ".join(c for c, _ in columns)
    arrays = ", ".join(
        f"${i}::{'text' if t == 'integer[]' else t}[]" for i, (_, t) in enumerate(columns, 2)
    )
    values = ", ".join(f"{c}::integer[]" if t == "integer[]" else c for c, t in columns)
    return (
        f"INSERT INTO {table} (project_id, {names}) "  # noqa: S608
        f"SELECT $1, {values} FROM unnest({arrays}) AS t({names}) "
        "ON CONFLICT DO NOTHING"
    )


_INSERT_SUMMARIES = _insert("summary_records", _SUMMARY_COLUMNS)
_INSERT_SAMPLES = _insert("call_samples", _SAMPLE_COLUMNS)
# Buckets whose dashboard totals may be built already, so need rebuilding (ADR 0023).
_MARK_LATE = (
    "INSERT INTO totals_stale (project_id, bucket) SELECT $1, unnest($2::timestamptz[]) "
    "ON CONFLICT DO NOTHING"
)


def _time(ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=ms)


def _int_array(values: list[int]) -> str:
    return "{" + ",".join(map(str, values)) + "}"


def _summary_row(s: Summary) -> dict[str, object]:
    return {
        **s.series.model_dump(),
        "minute": _time(s.minute),
        "summary_id": s.summary_id,
        "count": s.count,
        "ok": s.ok,
        "cancelled": s.cancelled,
        **{f"err_{c}": s.errors.get(c, 0) for c in ERROR_CLASSES},  # type: ignore[call-overload]
        "ttft_hist": _int_array(s.ttft_hist),
        "duration_hist": _int_array(s.duration_hist),
        "decode_hist": _int_array(s.decode_hist),
        "input_tokens_sum": s.input_tokens_sum,
        "output_tokens_sum": s.output_tokens_sum,
        "output_tokens_estimated_count": s.output_tokens_estimated_count,
    }


def _sample_row(s: Sample) -> dict[str, object]:
    return {**s.model_dump(), "ts": _time(s.ts), "input_bucket": s.input_bucket}


def _arrays(rows: list[dict[str, object]], columns: Columns) -> list[list[object]]:
    return [[row[c] for row in rows] for c, _ in columns]


class Sink(Protocol):
    async def write(
        self, project_id: UUID, summaries: Sequence[Summary], samples: Sequence[Sample]
    ) -> None: ...


class PostgresSink:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def write(
        self, project_id: UUID, summaries: Sequence[Summary], samples: Sequence[Sample]
    ) -> None:
        # No transaction: every record has an idempotent id, so if a later statement fails,
        # the SDK's retry of the whole batch only adds what is missing. Late marks come right
        # after their summaries, so a build that sees a mark also sees the summaries.
        summary_rows = [_summary_row(s) for s in summaries]
        late = late_buckets((_time(s.minute) for s in summaries), datetime.now(UTC))
        async with self._pool.acquire(timeout=DB_TIMEOUT_S) as conn:
            if summary_rows:
                await self._insert(
                    conn, _INSERT_SUMMARIES, project_id, summary_rows, _SUMMARY_COLUMNS
                )
            if late:
                await conn.execute(_MARK_LATE, project_id, late, timeout=DB_TIMEOUT_S)
            if samples:
                sample_rows = [_sample_row(s) for s in samples]
                await self._insert(conn, _INSERT_SAMPLES, project_id, sample_rows, _SAMPLE_COLUMNS)

    @staticmethod
    async def _insert(
        conn: asyncpg.Connection,
        sql: str,
        project_id: UUID,
        rows: list[dict[str, object]],
        columns: Columns,
    ) -> None:
        await conn.execute(sql, project_id, *_arrays(rows, columns), timeout=DB_TIMEOUT_S)
