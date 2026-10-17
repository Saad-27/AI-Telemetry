"""POST /v1/ingest into Postgres: rows, server fields, replays and revocation (ADR 0016)."""

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient
from records import envelope, gzipped, now_ms, sample, summary

from app.core.config import Settings
from app.db.dev_key import create_dev_key
from app.db.partitions import ensure_partitions
from app.ingest.schema import Envelope, validate_records
from app.ingest.sink import PostgresSink
from app.main import create_app

pytestmark = pytest.mark.db


def run(database_url: str, query: str, *args: Any) -> list[asyncpg.Record]:
    async def go() -> list[asyncpg.Record]:
        conn = await asyncpg.connect(database_url)
        try:
            return await conn.fetch(query, *args)  # type: ignore[no-any-return]
        finally:
            await conn.close()

    return asyncio.run(go())


def dev_key(database_url: str) -> tuple[uuid.UUID, str]:
    async def go() -> tuple[str, str]:
        conn = await asyncpg.connect(database_url)
        try:
            await ensure_partitions(conn, datetime.now(UTC).date())
            return await create_dev_key(conn)
        finally:
            await conn.close()

    project, key = asyncio.run(go())
    return uuid.UUID(project), key


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(database_url=database_url))) as client:
        yield client


def post(client: TestClient, key: str, body: dict[str, Any]) -> tuple[int, Any]:
    resp = client.post(
        "/v1/ingest",
        content=gzipped(body),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        },
    )
    return resp.status_code, resp.json()


def test_batch_is_stored_with_server_fields(client: TestClient, database_url: str) -> None:
    project, key = dev_key(database_url)
    s = summary()
    small = sample(input_tokens=500)
    failed = sample(
        reason="error",
        status="error",
        error_class="overloaded",
        http_status=529,
        provider_error_code="overloaded_error",
        ttft_ms=None,
        input_tokens=None,
    )
    assert post(client, key, envelope([s], [small, failed])) == (
        202,
        {"accepted": 3, "rejected": 0, "errors": []},
    )

    [row] = run(
        database_url, "SELECT * FROM summary_records WHERE summary_id = $1", s["summary_id"]
    )
    assert row["project_id"] == project
    assert row["minute"] == datetime.fromtimestamp(s["minute"] / 1000, UTC)
    assert (row["count"], row["ok"], row["cancelled"], row["err_overloaded"]) == (120, 117, 1, 2)
    assert row["err_unknown"] == 0
    assert row["ttft_hist"] == s["ttft_hist"] and row["decode_hist"] == s["decode_hist"]
    assert row["input_tokens_sum"] == 512340 and row["model"] == "claude-sonnet-x-20260801"
    assert row["received_at"] is not None

    rows = run(
        database_url,
        "SELECT event_id, project_id, input_bucket, error_class, ttft_ms, ts FROM call_samples "
        "WHERE event_id = ANY($1::uuid[]) ORDER BY input_bucket",
        [small["event_id"], failed["event_id"]],
    )
    assert [(str(r["event_id"]), r["input_bucket"], r["error_class"]) for r in rows] == [
        (failed["event_id"], "unknown", "overloaded"),
        (small["event_id"], "xs", None),
    ]
    assert all(r["project_id"] == project for r in rows)
    assert rows[1]["ttft_ms"] == pytest.approx(612.4, abs=0.01)
    assert rows[1]["ts"] == datetime.fromtimestamp(small["ts"] / 1000, UTC)


def test_replayed_batch_adds_nothing(client: TestClient, database_url: str) -> None:
    _, key = dev_key(database_url)
    batch = envelope([summary()], [sample()])
    assert post(client, key, batch)[0] == 202
    assert post(client, key, batch)[0] == 202  # an SDK retry after a lost response
    ids = [batch["summaries"][0]["summary_id"], batch["samples"][0]["event_id"]]
    [(n,)] = run(
        database_url,
        "SELECT (SELECT count(*) FROM summary_records WHERE summary_id = $1::uuid)"
        " + (SELECT count(*) FROM call_samples WHERE event_id = $2::uuid)",
        *ids,
    )
    assert n == 2


def test_revoked_key_is_refused(client: TestClient, database_url: str) -> None:
    _, key = dev_key(database_url)
    run(
        database_url,
        "UPDATE ingest_keys SET revoked_at = now() WHERE display_prefix = $1 AND last4 = $2",
        key[:12],
        key[-4:],
    )
    assert post(client, key, envelope(samples=[sample()]))[0] == 401


def test_dev_key_reuses_the_dev_project(database_url: str) -> None:
    (first, key1), (second, key2) = dev_key(database_url), dev_key(database_url)
    assert first == second and key1 != key2


def test_records_on_both_sides_of_midnight(client: TestClient, database_url: str) -> None:
    # Partitions are per UTC day; a batch can span two of them.
    _, key = dev_key(database_url)
    midnight = now_ms() // 86_400_000 * 86_400_000
    batch = envelope(samples=[sample(ts=midnight - 1), sample(ts=min(midnight + 1, now_ms()))])
    assert post(client, key, batch) == (202, {"accepted": 2, "rejected": 0, "errors": []})


def test_sink_works_as_ingest_writer(database_url: str) -> None:
    # The app connects as the owner until hosting (step 8); this is what it will connect as.
    batch = Envelope.model_validate(envelope([summary()], [sample()]))
    records = validate_records(batch, now_ms())

    async def go() -> None:
        async def as_ingest_writer(conn: asyncpg.Connection) -> None:
            await conn.execute("SET ROLE ingest_writer")

        pool = await asyncpg.create_pool(
            database_url, min_size=0, max_size=1, setup=as_ingest_writer
        )
        try:
            for _ in range(2):  # the replay hits ON CONFLICT, which needs no SELECT grant
                await PostgresSink(pool).write(uuid.uuid4(), records.summaries, records.samples)
        finally:
            await pool.close()

    asyncio.run(go())
