"""The load generator's bodies are what ingest accepts (tools/loadtest, docs/capacity.md), so a
schema change cannot quietly turn a load test into a test of rejections."""

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
