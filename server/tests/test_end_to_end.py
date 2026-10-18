"""Example apps to dashboard sums, end to end (brief §14 M3, ADR 0022).

Every example in examples/ runs in the SDK's own environment and sends to this app, served by
uvicorn on a free port, over real HTTP. Debug mode prints exactly what was sent, so the test
knows what to expect: every record stored, and the dashboard's sums equal to the payloads',
read from the summaries and again from built totals (ADR 0023).
A record the server refuses (an enum or host copy that drifted from the SDK's) is missing.
`test_server_accepts_what_the_sdk_can_send` compares those copies directly.
"""

import asyncio
import json
import os
import socket
import subprocess
import threading
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, get_args
from uuid import UUID

import asyncpg
import pytest
import uvicorn

from app.core.config import Settings
from app.dashboard.repository import Filters, MetricsRepository, Sums
from app.db.partitions import ensure_partitions
from app.db.totals import FIVE_MINUTES, HOUR, build, built_through, ceil, floor
from app.ingest.keys import generate
from app.ingest.schema import DECODE_BOUNDS_TPS, LATENCY_BOUNDS_MS, Provider, Route, Series
from app.main import create_app

REPO = Path(__file__).resolve().parents[2]
SDK = REPO / "sdk" / "python"
EXAMPLES = sorted((REPO / "examples").glob("[!_]*.py"))
PREFIX = "[gaslit] payload "
MINUTE = timedelta(minutes=1)

Record = dict[str, Any]


def sdk_python(*args: str, env: dict[str, str] | None = None) -> subprocess.Popen[str]:
    """Python in the SDK's locked environment, which has gaslit, openai and anthropic."""
    environ = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}  # this one is ours
    return subprocess.Popen(
        [os.environ.get("UV", "uv"), "run", "--locked", "--quiet", "python", *args],
        cwd=SDK,
        env={**environ, **(env or {})},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def finish(process: subprocess.Popen[str]) -> tuple[str, str]:
    out, err = process.communicate(timeout=180)
    assert process.returncode == 0, err
    return out, err


@pytest.fixture(scope="module")
def server(database_url: str) -> Iterator[str]:
    """The whole app, as deployed, on a free local port."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    app = create_app(Settings(database_url=database_url))
    uv = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=uv.run, kwargs={"sockets": [sock]})
    thread.start()
    try:
        while not uv.started:
            assert thread.is_alive(), "the server did not start"
            thread.join(0.02)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        uv.should_exit = True
        thread.join()
        sock.close()


async def new_account(database_url: str) -> tuple[UUID, str]:
    """A user, project and key of its own: other tests' rows stay out of the sums."""
    key = generate()
    conn = await asyncpg.connect(database_url)
    try:
        await ensure_partitions(conn, datetime.now(UTC).date())
        project = await conn.fetchval(
            """
            WITH u AS (INSERT INTO users (email) VALUES ('e2e@example.com') RETURNING id),
            p AS (INSERT INTO projects (name, owner_user_id) SELECT 'e2e', id FROM u RETURNING id)
            INSERT INTO ingest_keys (project_id, key_hash, display_prefix, last4)
            SELECT id, $1, $2, $3 FROM p RETURNING project_id
            """,
            key.key_hash,
            key.display_prefix,
            key.last4,
        )
    finally:
        await conn.close()
    return project, key.key


@pytest.fixture(scope="module")
def sent(server: str, database_url: str) -> tuple[UUID, list[Record], list[Record]]:
    """Runs every example against the server. Returns the project, and the summaries and
    samples the examples printed as sent, each once (a retried batch is printed again)."""
    project, key = asyncio.run(new_account(database_url))
    env = {"GASLIT_ENDPOINT": server, "GASLIT_KEY": key}
    running = [sdk_python(str(path), env=env) for path in EXAMPLES]
    summaries: dict[str, Record] = {}
    samples: dict[str, Record] = {}
    for process in running:
        _, err = finish(process)
        for line in err.splitlines():
            if line.startswith(PREFIX):
                payload = json.loads(line.removeprefix(PREFIX))
                summaries.update((s["summary_id"], s) for s in payload["summaries"])
                samples.update((s["event_id"], s) for s in payload["samples"])
            else:  # the SDK's warnings, such as a refused key or a dropped batch
                assert not line.startswith("[gaslit] "), err
    assert summaries and samples
    return project, list(summaries.values()), list(samples.values())


@pytest.fixture
async def repo(database_url: str) -> AsyncIterator[MetricsRepository]:
    async def as_dashboard_app(conn: asyncpg.Connection) -> None:
        await conn.execute("SET ROLE dashboard_app")

    pool = await asyncpg.create_pool(database_url, min_size=0, max_size=1, setup=as_dashboard_app)
    try:
        yield MetricsRepository(pool)
    finally:
        await pool.close()


def total(summaries: list[Record]) -> Sums:
    """The payloads added up by hand, as the dashboard should."""
    errors: Counter[str] = Counter()
    for s in summaries:
        errors.update(s["errors"])

    def hist(name: str) -> tuple[int, ...]:
        return tuple(map(sum, zip(*(s[name] for s in summaries), strict=True)))

    return Sums(
        count=sum(s["count"] for s in summaries),
        ok=sum(s["ok"] for s in summaries),
        cancelled=sum(s["cancelled"] for s in summaries),
        errors={c: n for c, n in errors.items() if n},
        ttft_hist=hist("ttft_hist"),
        duration_hist=hist("duration_hist"),
        decode_hist=hist("decode_hist"),
        input_tokens_sum=sum(s["input_tokens_sum"] for s in summaries),
        output_tokens_sum=sum(s["output_tokens_sum"] for s in summaries),
        output_tokens_estimated_count=sum(s["output_tokens_estimated_count"] for s in summaries),
    )


def group(summaries: list[Record], *keys: str) -> dict[tuple[Any, ...], Sums]:
    groups: defaultdict[tuple[Any, ...], list[Record]] = defaultdict(list)
    for s in summaries:
        when = {"minute": minute, "five_minutes": five_minutes}
        groups[tuple(when[k](s) if k in when else s["series"][k] for k in keys)].append(s)
    return {k: total(v) for k, v in groups.items()}


def minute(s: Record) -> datetime:
    return datetime.fromtimestamp(s["minute"] / 1000, UTC)


def five_minutes(s: Record) -> datetime:
    return floor(minute(s), FIVE_MINUTES)


def span(summaries: list[Record]) -> tuple[datetime, datetime]:
    return min(map(minute, summaries)), max(map(minute, summaries)) + MINUTE


@pytest.mark.db
def test_every_record_sent_is_stored(
    sent: tuple[UUID, list[Record], list[Record]], database_url: str
) -> None:
    project, summaries, samples = sent

    async def stored() -> tuple[int, int]:
        conn = await asyncpg.connect(database_url)
        try:
            row = await conn.fetchrow(
                "SELECT (SELECT count(*) FROM summary_records WHERE project_id = $1),"
                " (SELECT count(*) FROM call_samples WHERE project_id = $1)",
                project,
            )
        finally:
            await conn.close()
        return row[0], row[1]

    assert asyncio.run(stored()) == (len(summaries), len(samples))
    # Every provider and route the examples use made it through.
    assert {(s["series"]["provider"], s["series"]["route"]) for s in summaries} >= {
        ("openai", "direct"), ("anthropic", "direct"), ("openai", "azure"),
        ("anthropic", "bedrock"), ("anthropic", "vertex"), ("custom", "custom"),
        ("local", "local"),
    }  # fmt: skip


@pytest.mark.db
@pytest.mark.anyio
async def test_overview_equals_the_payloads(
    sent: tuple[UUID, list[Record], list[Record]], repo: MetricsRepository
) -> None:
    project, summaries, _ = sent
    rows = await repo.overview(project, *span(summaries))
    assert {(r.provider, r.route, r.model): r.sums for r in rows} == group(
        summaries, "provider", "route", "model"
    )
    chat = [s for s in summaries if s["series"]["service"] == "chat-demo"]
    rows = await repo.overview(project, *span(summaries), Filters(service="chat-demo"))
    assert {(r.provider, r.route, r.model): r.sums for r in rows} == group(
        chat, "provider", "route", "model"
    )


@pytest.mark.db
@pytest.mark.anyio
async def test_series_equal_the_payloads(
    sent: tuple[UUID, list[Record], list[Record]], repo: MetricsRepository
) -> None:
    project, summaries, _ = sent
    for (provider, route, model), _ in group(summaries, "provider", "route", "model").items():
        mine = [
            s
            for s in summaries
            if (s["series"]["provider"], s["series"]["route"], s["series"]["model"])
            == (provider, route, model)
        ]
        filters = Filters(provider=provider, route=route, model=model)
        points = await repo.series(project, *span(summaries), MINUTE, filters)
        assert {(p.start, p.input_bucket): p.sums for p in points} == group(
            mine, "minute", "input_bucket"
        )


@pytest.mark.db
@pytest.mark.anyio
async def test_sums_are_the_same_from_totals(
    sent: tuple[UUID, list[Record], list[Record]], repo: MetricsRepository, database_url: str
) -> None:
    project, summaries, _ = sent
    first, last = span(summaries)
    start, end = floor(first, HOUR), ceil(last, FIVE_MINUTES)
    conn = await asyncpg.connect(database_url)
    try:
        before = await built_through(conn)
        # Built as if 10 minutes later, so the examples' buckets are built too.
        await conn.execute("UPDATE totals_watermark SET built_through = $1", start)
        await build(conn, end + 10 * timedelta(minutes=1))
        rows = await repo.overview(project, start, end)
        assert {(r.provider, r.route, r.model): r.sums for r in rows} == group(
            summaries, "provider", "route", "model"
        )
        points = await repo.series(project, start, end, FIVE_MINUTES)
        assert {(p.start, p.input_bucket): p.sums for p in points} == group(
            summaries, "five_minutes", "input_bucket"
        )
    finally:
        # Back to what is really built: the buckets built early are rebuilt in due course.
        await conn.execute("UPDATE totals_watermark SET built_through = $1", before)
        await conn.close()


# Checked against the SDK's own tables, so a host or enum added there must be added here.
_SDK_FACTS = """
import json
from gaslit import _hosts, _model

hosts = [
    *_hosts._FIXED,
    *("customer." + suffix for suffix in _hosts._AZURE_SUFFIXES),
    "bedrock-runtime.eu-central-1.amazonaws.com",
    "bedrock-runtime-fips.us-gov-west-1.amazonaws.com",
    "aiplatform.googleapis.com",
    "us-east5-aiplatform.googleapis.com",
    "aiplatform.eu.rep.googleapis.com",
]
print(json.dumps({
    "providers": sorted(_model.PROVIDERS),
    "routes": sorted(_model.ROUTES),
    "latency_bounds": _model.LATENCY_BOUNDS_MS,
    "decode_bounds": _model.DECODE_BOUNDS_TPS,
    "endpoints": [_hosts.detect("https://" + host, "anthropic.claude-x") for host in hosts],
}))
"""


def test_server_accepts_what_the_sdk_can_send() -> None:
    out, _ = finish(sdk_python("-c", _SDK_FACTS))
    sdk = json.loads(out)
    assert sorted(get_args(Provider)) == sdk["providers"]
    assert sorted(get_args(Route)) == sdk["routes"]
    assert list(LATENCY_BOUNDS_MS) == sdk["latency_bounds"]
    assert list(DECODE_BOUNDS_TPS) == sdk["decode_bounds"]
    for provider, route, host, region in sdk["endpoints"]:
        Series.model_validate(
            {
                "provider": provider,
                "route": route,
                "endpoint_host": host,
                "endpoint_region": region,
                "operation": "messages",
                "model": "m",
                "streaming": False,
                "input_bucket": "xs",
                "service": None,
                "environment": None,
                "origin_region": None,
            }
        )
