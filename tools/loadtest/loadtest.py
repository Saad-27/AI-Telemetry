"""Load generator for POST /v1/ingest (brief §20.6, ADR 0024). Results go in docs/capacity.md.

    setup  accounts, projects and keys in the database at DATABASE_URL (as the schema owner),
           and a week of summaries for two of them, so dashboard reads have history to sum
    run    sends traffic at fixed rates, then prints status codes and latency per interval and
           per step, and optionally the CPU of Docker containers and the database's WAL

Open loop: each request leaves at its scheduled time whether or not earlier ones have been
answered, and its latency counts from that time. A slow server shows up as latency, not as a
lower request rate.

Bodies are built like the SDK's (server/tests/records.py), with new ids each time, so every
record is really inserted. Run from server/ with PYTHONPATH=.:tests (`make loadtest`).
"""

import argparse
import asyncio
import gzip
import json
import multiprocessing
import os
import random
import secrets
import subprocess
import threading
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import asyncpg
from app.dashboard.repository import MetricsRepository
from app.db.partitions import ensure_partitions
from app.db.totals import build
from app.ingest.keys import generate
from records import envelope, sample, summary

MODELS = ("claude-sonnet-x-20260801", "claude-haiku-x-20260801", "claude-opus-x-20260801")
SERVICES = ("api", "worker", "summariser")
SUMMARIES_PER_MINUTE = 4  # series an app process has; one batch a minute carries them
BATCHES_PER_MINUTE = 12  # the SDK sends every 5 s when it has samples
JUNK_ACCOUNTS = 10  # junk comes from a few accounts of its own, as from an abuser


# Request bodies ------------------------------------------------------------------------------


def _series() -> dict[str, Any]:
    return {"model": random.choice(MODELS), "service": random.choice(SERVICES)}


def _sample() -> dict[str, Any]:
    s = _series()
    return sample(model_requested=s["model"], model_returned=s["model"], service=s["service"])


def _summary() -> dict[str, Any]:
    base = summary()
    return summary(series={**base["series"], **_series()})


def normal_body() -> tuple[bytes, int]:
    """One SDK process's traffic: a batch of 1-3 samples every 5 s, and once a minute its
    series' summaries with a sample. About 2 records a request."""
    if random.randrange(BATCHES_PER_MINUTE) == 0:
        summaries = [_summary() for _ in range(SUMMARIES_PER_MINUTE)]
        samples = [_sample()]
    else:
        summaries, samples = [], [_sample() for _ in range(random.randint(1, 3))]
    return _gzip(envelope(summaries, samples)), len(summaries) + len(samples)


def large_body() -> tuple[bytes, int]:
    """A full SDK batch (max_batch 200), as sent after a backlog."""
    return _gzip(envelope([_summary() for _ in range(50)], [_sample() for _ in range(150)])), 200


def _gzip(body: dict[str, Any]) -> bytes:
    return gzip.compress(json.dumps(body, separators=(",", ":")).encode(), mtime=0)


def _junk_bodies() -> list[bytes]:
    """Rejected payloads: malformed JSON, a zip bomb (6 MB of zeros, ~6 KB sent) and 1,001
    records (413 after parsing, the costliest rejection)."""
    many = envelope([], [sample() for _ in range(1001)])
    return [_gzip_raw(b"{not json"), _gzip_raw(bytes(6_000_000)), _gzip(many)]


def _gzip_raw(data: bytes) -> bytes:
    return gzip.compress(data, mtime=0)


# HTTP over asyncio streams: no client library, so the generator stays cheap ------------------


class Client:
    """Keep-alive connections, at most `conns` at once, like Fly's proxy in front of the app.
    A request waiting for a connection is still on the clock."""

    def __init__(self, url: str, conns: int, timeout_s: float) -> None:
        parts = urlsplit(url)
        self._host, self._port = parts.hostname or "127.0.0.1", parts.port or 80
        self._head = f"POST /v1/ingest HTTP/1.1\r\nHost: {parts.netloc}\r\n"
        self._slots = asyncio.Semaphore(conns)
        self._idle: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []
        self._timeout_s = timeout_s

    async def post(self, key: str, body: bytes) -> int:
        request = (
            f"{self._head}Authorization: Bearer {key}\r\nContent-Type: application/json\r\n"
            f"Content-Encoding: gzip\r\nContent-Length: {len(body)}\r\n\r\n"
        ).encode() + body
        # The SDK's timeout covers the whole request, waiting for a connection included.
        return await asyncio.wait_for(self._post(request), self._timeout_s)

    async def _post(self, request: bytes) -> int:
        async with self._slots:
            while self._idle:  # the server closes connections idle for 5 s: retry on a new one
                conn = self._idle.pop()
                try:
                    return await self._send(conn, request)
                except ConnectionResetError:
                    continue
            conn = await asyncio.open_connection(self._host, self._port)
            return await self._send(conn, request)

    async def _send(
        self, conn: tuple[asyncio.StreamReader, asyncio.StreamWriter], request: bytes
    ) -> int:
        try:
            status, keep = await self._exchange(conn, request)
        except BaseException:
            conn[1].close()
            raise
        if keep:
            self._idle.append(conn)
        else:
            conn[1].close()
        return status

    @staticmethod
    async def _exchange(
        conn: tuple[asyncio.StreamReader, asyncio.StreamWriter], request: bytes
    ) -> tuple[int, bool]:
        reader, writer = conn
        writer.write(request)
        await writer.drain()
        line = await reader.readline()
        if not line:  # closed before answering
            raise ConnectionResetError
        status = int(line.split()[1])
        length, keep = 0, True
        while (line := await reader.readline()) not in (b"\r\n", b""):
            name, _, value = line.decode("latin-1").partition(":")
            name = name.strip().lower()
            if name == "content-length":
                length = int(value)
            elif name == "connection" and value.strip().lower() == "close":
                keep = False
        await reader.readexactly(length)
        return status, keep


# Sending -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Stream:
    """Requests of one kind: `rates` (requests/s, all processes together), one per step."""

    kind: str  # normal, large, unknown-key, bad-key, junk
    rates: list[float]


# (stream, scheduled offset s, latency s, status or 0 for a network error, records sent)
Result = tuple[str, float, float, int, int]


def _worker(
    url: str, keys: list[str], streams: list[Stream], step_s: float, args: dict[str, Any]
) -> list[Result]:
    random.seed(os.getpid())
    return asyncio.run(_send_all(url, keys, streams, step_s, args))


async def _send_all(
    url: str, keys: list[str], streams: list[Stream], step_s: float, args: dict[str, Any]
) -> list[Result]:
    client = Client(url, args["conns"], args["timeout_s"])
    loop = asyncio.get_running_loop()
    start = loop.time() + (args["start"] - time.time())  # the same instant in every process
    junk = _junk_bodies()
    results: list[Result] = []
    pending: set[asyncio.Task[None]] = set()

    junk_keys, keys = keys[-JUNK_ACCOUNTS:], keys[:-JUNK_ACCOUNTS]

    def body(kind: str) -> tuple[str, bytes, int]:
        if kind == "large":
            return random.choice(keys), *large_body()
        if kind == "junk":
            return random.choice(junk_keys), random.choice(junk), 0
        data, records = normal_body()
        if kind == "unknown-key":  # well formed, so each one is looked up in the database
            return generate().key, data, records
        if kind == "bad-key":  # fails the checksum, so no database work
            return "gl_live_" + secrets.token_hex(25)[:49], data, records
        return random.choice(keys), data, records

    async def one(kind: str, at: float) -> None:
        key, data, records = body(kind)
        try:
            status = await client.post(key, data)
        except (OSError, TimeoutError, asyncio.IncompleteReadError, ValueError, IndexError):
            status = 0
        results.append((kind, at - start, loop.time() - at, status, records))

    async def run(stream: Stream) -> None:
        offset = random.random()  # spreads the processes' schedules apart
        step_start = start
        for rate in stream.rates:
            rate /= args["procs"]
            if rate > 0:
                k = 0
                while (at := step_start + (k + offset) / rate) < step_start + step_s:
                    if (delay := at - loop.time()) > 0:
                        await asyncio.sleep(delay)
                    task = asyncio.create_task(one(stream.kind, at))
                    pending.add(task)
                    task.add_done_callback(pending.discard)
                    k += 1
                    if delay <= 0:
                        await asyncio.sleep(0)
            step_start += step_s

    await asyncio.gather(*(run(s) for s in streams))
    await asyncio.gather(*list(pending))
    return results


# Measuring the containers and the database --------------------------------------------------


def _cpu_s(container: str) -> float:
    """CPU seconds a container has used (cgroup v2)."""
    out = subprocess.run(
        ["docker", "exec", container, "cat", "/sys/fs/cgroup/cpu.stat"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return int(dict(line.split() for line in out.splitlines())["usage_usec"]) / 1e6


def _memory_peak_mb(container: str) -> float | None:
    out = subprocess.run(
        ["docker", "exec", container, "cat", "/sys/fs/cgroup/memory.peak"],
        capture_output=True,
        text=True,
    ).stdout
    return int(out) / 2**20 if out.strip().isdigit() else None


async def _wal_bytes(database_url: str) -> int:
    conn = await asyncpg.connect(database_url)
    try:
        return int(await conn.fetchval("SELECT wal_bytes FROM pg_stat_wal"))
    finally:
        await conn.close()


async def _dashboard_reads(
    database_url: str, project: uuid.UUID, until: float, every_s: float
) -> dict[str, list[float]]:
    """The dashboard's reads for the busy account, as dashboard_app, while the load runs."""

    async def as_dashboard_app(conn: asyncpg.Connection) -> None:
        await conn.execute("SET ROLE dashboard_app")

    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=1, setup=as_dashboard_app)
    repo = MetricsRepository(pool)
    times: dict[str, list[float]] = defaultdict(list)
    try:
        while time.time() < until:
            end = datetime.now(UTC).replace(second=0, microsecond=0)
            for name, span, step in (
                ("1h", timedelta(hours=1), timedelta(minutes=1)),
                ("24h", timedelta(hours=24), timedelta(minutes=15)),
                ("7d", timedelta(days=7), timedelta(hours=1)),
            ):
                for query in ("overview", "series"):
                    started = time.perf_counter()
                    if query == "overview":
                        await repo.overview(project, end - span, end)
                    else:
                        await repo.series(project, end - span, end, step)
                    times[f"{query} {name}"].append(time.perf_counter() - started)
            await asyncio.sleep(every_s)
    finally:
        await pool.close()
    return times


# Reporting -----------------------------------------------------------------------------------


def _pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


_stop = threading.Event()


def _sample_cpu(
    containers: list[str], start: float, every_s: float, out: list[list[float]]
) -> None:
    """CPU seconds of each container at start + k * every_s, in a thread while the load runs."""
    k = 0
    while not _stop.is_set():
        _stop.wait(max(0.0, start + k * every_s - time.time()))
        out.append([_cpu_s(c) for c in containers])
        k += 1


def _line(label: str, rows: list[Result], seconds: float) -> str:
    statuses = Counter(r[3] for r in rows)
    latencies = [r[2] * 1000 for r in rows]
    stored = sum(r[4] for r in rows if r[3] == 202)
    codes = " ".join(f"{s or 'err'}={n}" for s, n in sorted(statuses.items()))
    return (
        f"{label:<24} {len(rows) / seconds:7.0f} req/s {stored / seconds:7.0f} rec/s  "
        f"p50 {_pct(latencies, 0.5):6.1f}  p95 {_pct(latencies, 0.95):6.1f}  "
        f"p99 {_pct(latencies, 0.99):6.1f}  max {max(latencies, default=0):6.0f} ms  {codes}"
    )


def _report(
    results: list[Result],
    streams: list[Stream],
    step_s: float,
    every_s: float,
    cpu: list[list[float]],
) -> None:
    by_stream: dict[str, list[Result]] = defaultdict(list)
    for r in results:
        by_stream[r[0]].append(r)
    for stream in streams:
        rows = by_stream[stream.kind]
        print(f"\n== {stream.kind}, every {every_s:g} s (latency from the scheduled time)")
        buckets: dict[int, list[Result]] = defaultdict(list)
        for r in rows:
            buckets[int(r[1] // every_s)].append(r)
        for i in sorted(buckets):
            used = ""
            if i + 1 < len(cpu):
                used = " cpu " + "/".join(
                    f"{(b - a) / every_s:.2f}" for a, b in zip(*cpu[i : i + 2], strict=True)
                )
            print(_line(f"  t={i * every_s:5.0f}s", buckets[i], every_s) + used)
        print(f"== {stream.kind}, per step")
        for i, rate in enumerate(stream.rates):
            step = [r for r in rows if i * step_s <= r[1] < (i + 1) * step_s]
            print(_line(f"  step {i + 1} @ {rate:g}/s", step, step_s))


# Commands ------------------------------------------------------------------------------------


def _rates(text: str) -> list[float]:
    return [float(x) for x in text.split(",")]


def run(args: argparse.Namespace) -> None:
    lines = Path(args.keys).read_text().split()
    projects, keys = lines[0::2], lines[1::2]
    streams = [Stream(args.kind, _rates(args.rate))]
    for flood in args.flood or []:
        kind, _, rate = flood.partition(":")
        streams.append(Stream(kind, [float(rate)] * len(streams[0].rates)))
    steps = len(streams[0].rates)
    start = time.time() + 2  # time for the processes to start
    duration = steps * args.step_s
    shared = {"conns": args.conns, "timeout_s": args.timeout_s, "procs": args.procs, "start": start}
    database_url = os.environ.get("DATABASE_URL")

    cpu_before = {c: _cpu_s(c) for c in args.containers}
    cpu: list[list[float]] = []
    sampler = threading.Thread(
        target=_sample_cpu, args=(args.containers, start, args.every_s, cpu), daemon=True
    )
    if args.containers:
        sampler.start()
    wal_before = asyncio.run(_wal_bytes(database_url)) if database_url else None
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(args.procs) as pool:
        pending = pool.starmap_async(
            _worker, [(args.url, keys, streams, args.step_s, shared)] * args.procs
        )
        reads = (
            asyncio.run(
                _dashboard_reads(
                    database_url, uuid.UUID(projects[0]), start + duration, args.dashboard_every_s
                )
            )
            if args.dashboard and database_url
            else {}
        )
        results = [r for part in pending.get() for r in part]
    elapsed = time.time() - start
    _stop.set()

    print(
        f"{len(results)} requests in {elapsed:.0f} s, "
        f"{args.procs} processes, {args.conns} connections each"
    )
    if args.containers:
        print(f"cpu per interval: {' / '.join(args.containers)} (1.00 = one core)")
    _report(results, streams, args.step_s, args.every_s, cpu)
    if args.containers:
        print("\n== containers: CPU over the run (1.0 = one core), memory peak since start")
        for c in args.containers:
            peak = _memory_peak_mb(c)
            used = (_cpu_s(c) - cpu_before[c]) / elapsed
            print(f"  {c:<20} cpu {used:5.2f}   memory peak {peak or float('nan'):6.0f} MB")
    if wal_before is not None:
        wal = asyncio.run(_wal_bytes(database_url)) - wal_before
        print(f"\n== database WAL: {wal / 2**20:.0f} MB, {wal / 2**20 / elapsed:.2f} MB/s")
    if reads:
        print("\n== dashboard reads during the run, busy account (ms)")
        for name, times in reads.items():
            ms = [t * 1000 for t in times]
            print(
                f"  {name:<14} n={len(ms):3}  p50 {_pct(ms, 0.5):6.1f}  p95 {_pct(ms, 0.95):6.1f}"
            )


async def setup(args: argparse.Namespace) -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        today = datetime.now(UTC).date()
        await ensure_partitions(conn, today - timedelta(days=8), days_ahead=16)
        users = await conn.fetch(
            "INSERT INTO users (email, display_name) "
            "SELECT 'load' || i || '@localhost', 'Load' FROM generate_series(1, $1) i "
            "RETURNING id",
            args.accounts,
        )
        user_ids = [u["id"] for u in users]
        projects = await conn.fetch(
            "INSERT INTO projects (name, owner_user_id) SELECT 'load', u FROM unnest($1::uuid[]) u "
            "RETURNING id, owner_user_id",
            user_ids,
        )
        await conn.execute(
            "INSERT INTO project_members (project_id, user_id) "
            "SELECT * FROM unnest($1::uuid[], $2::uuid[])",
            [p["id"] for p in projects],
            [p["owner_user_id"] for p in projects],
        )
        new = [generate() for _ in projects]
        await conn.execute(
            "INSERT INTO ingest_keys (project_id, key_hash, display_prefix, last4, created_by) "
            "SELECT * FROM unnest($1::uuid[], $2::bytea[], $3::text[], $4::text[], $5::uuid[])",
            [p["id"] for p in projects],
            [k.key_hash for k in new],
            [k.display_prefix for k in new],
            [k.last4 for k in new],
            [p["owner_user_id"] for p in projects],
        )
        path = Path(args.keys)
        path.touch(mode=0o600)
        path.write_text("".join(f"{p['id']} {k.key}\n" for p, k in zip(projects, new, strict=True)))
        # History, as measured in ADR 0022: a busy account (12 summaries a minute, 121k a
        # week) and a typical one (2 a minute, 20k).
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        week = now - timedelta(days=7)
        for project, per_minute in ((projects[0]["id"], 12), (projects[1]["id"], 2)):
            await conn.execute(_HISTORY, project, week, now, per_minute)
        await conn.execute("UPDATE totals_watermark SET built_through = $1", week)
        await build(conn, now, catch_up=True)
    finally:
        await conn.close()
    print(f"{args.accounts} accounts; keys in {args.keys} (busy account first)")


# Shaped like server/tests/records.py's summary; series vary by model and service.
_HISTORY = """
    INSERT INTO summary_records (project_id, minute, summary_id, provider, route, endpoint_host,
        operation, model, streaming, input_bucket, service, environment, origin_region, count,
        ok, cancelled, err_overloaded, ttft_hist, duration_hist, decode_hist, input_tokens_sum,
        output_tokens_sum, output_tokens_estimated_count)
    SELECT $1, m, gen_random_uuid(), 'anthropic', 'direct', 'api.anthropic.com', 'messages',
        'model-' || (s % 3), true, (ARRAY['xs', 's', 'l'])[s % 3 + 1], 'svc-' || (s % 4),
        'prod', 'fra', 120, 117, 1, 2,
        '{0,0,0,0,1,3,12,40,59,3,0,0,0,0,0,0,0,0,0,0}',
        '{0,0,0,0,0,0,0,0,0,0,2,9,51,43,11,1,0,0,0,0}',
        '{0,0,0,0,1,6,48,55,7,0,0,0,0,0}', 512340, 48211, 0
    FROM generate_series($2::timestamptz, $3::timestamptz - interval '1 minute',
                         interval '1 minute') m,
         generate_series(1, $4) s
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--accounts", type=int, default=1000)
    s.add_argument("--keys", required=True, help="file to write project ids and keys to")
    r = sub.add_parser("run")
    r.add_argument("--url", default="http://127.0.0.1:8001")
    r.add_argument("--keys", required=True)
    r.add_argument("--kind", default="normal", help="normal, large, unknown-key, bad-key, junk")
    r.add_argument("--rate", required=True, help="requests/s per step, e.g. 50,500,50")
    r.add_argument("--step-s", type=float, default=30)
    r.add_argument("--flood", action="append", help="KIND:RATE alongside, e.g. unknown-key:500")
    r.add_argument("--procs", type=int, default=4)
    r.add_argument("--conns", type=int, default=64, help="per process")
    r.add_argument("--timeout-s", type=float, default=5.0, help="the SDK's timeout")
    r.add_argument("--every-s", type=float, default=5.0)
    r.add_argument("--containers", nargs="*", default=[], help="Docker containers to measure")
    r.add_argument("--dashboard", action="store_true", help="time dashboard reads meanwhile")
    r.add_argument("--dashboard-every-s", type=float, default=5.0, help="pause between rounds")
    args = parser.parse_args()
    if args.command == "setup":
        asyncio.run(setup(args))
    else:
        run(args)


if __name__ == "__main__":
    main()
