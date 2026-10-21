"""The load generator's bodies are what ingest accepts (tools/loadtest, docs/capacity.md), so a
schema change cannot quietly turn a load test into a test of rejections."""

import asyncio
import gzip
import importlib.util
import time
from pathlib import Path
from types import ModuleType

import pydantic_core
import pytest

from app.ingest.routes import MAX_RECORDS
from app.ingest.schema import Envelope, validate_records

_TOOL = Path(__file__).resolve().parents[2] / "tools" / "loadtest" / "loadtest.py"


@pytest.fixture(scope="module")
def loadtest() -> ModuleType:
    spec = importlib.util.spec_from_file_location("loadtest", _TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("kind", ["normal_body", "large_body"])
def test_bodies_are_accepted_in_full(loadtest: ModuleType, kind: str) -> None:
    for _ in range(50):  # normal bodies vary: samples only, or a minute's summaries
        body, count = getattr(loadtest, kind)()
        envelope = Envelope.model_validate(pydantic_core.from_json(gzip.decompress(body)))
        records = validate_records(envelope, now_ms=time.time_ns() // 1_000_000)
        assert records.rejected == 0
        assert len(records.summaries) + len(records.samples) == count <= MAX_RECORDS


def test_client_sends_the_edge_header_and_speaks_https(loadtest: ModuleType) -> None:
    seen: list[bytes] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        seen.append(head + await reader.readexactly(len(b"body")))
        writer.write(b"HTTP/1.1 202 Accepted\r\nContent-Length: 2\r\n\r\n{}")
        await writer.drain()
        writer.close()  # else the server waits for this connection on exit

    async def main() -> int:
        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            client = loadtest.Client(f"http://127.0.0.1:{port}", 1, 5.0, "edge-value")
            return int(await client.post("gl_live_x", b"body"))

    assert asyncio.run(main()) == 202
    [request] = seen
    assert b"\r\nX-Gaslit-Edge: edge-value\r\n" in request and request.endswith(b"body")
    https = loadtest.Client("https://gaslit-api-production.fly.dev", 1, 5.0)
    assert https._port == 443 and https._ssl is not None
    assert loadtest.Client("http://127.0.0.1:8001", 1, 5.0)._ssl is None
