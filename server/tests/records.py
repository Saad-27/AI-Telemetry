"""Records shaped as the SDK sends them (sdk/python/src/gaslit/_model.py, _aggregator.py)."""

import gzip
import json
import time
import uuid
from typing import Any

Record = dict[str, Any]


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def sample(**changes: Any) -> Record:
    record: Record = {
        "event_id": str(uuid.uuid4()),
        "reason": "random",
        "ts": now_ms() - 5_000,
        "duration_ms": 2843.1,
        "ttft_ms": 612.4,
        "provider": "anthropic",
        "route": "direct",
        "endpoint_host": "api.anthropic.com",
        "endpoint_region": None,
        "operation": "messages",
        "model_requested": "claude-sonnet-x",
        "model_returned": "claude-sonnet-x-20260801",
        "streaming": True,
        "has_tools": False,
        "input_tokens": 18234,
        "output_tokens": 412,
        "cached_input_tokens": 0,
        "output_tokens_estimated": False,
        "status": "ok",
        "error_class": None,
        "http_status": 200,
        "provider_error_code": None,
        "service": "summariser",
        "environment": "prod",
        "label": None,
        "origin_region": "lhr",
        "provider_sdk": "anthropic",
        "provider_sdk_version": "0.50.0",
    }
    record.update(changes)
    return record


def summary(**changes: Any) -> Record:
    """The brief §19.2 example, for the previous minute."""
    minute = now_ms() // 60_000 * 60_000 - 60_000
    record: Record = {
        "summary_id": str(uuid.uuid4()),
        "minute": minute,
        "series": {
            "provider": "anthropic",
            "route": "direct",
            "endpoint_host": "api.anthropic.com",
            "endpoint_region": None,
            "operation": "messages",
            "model": "claude-sonnet-x-20260801",
            "streaming": True,
            "input_bucket": "s",
            "service": "summariser",
            "environment": "prod",
            "origin_region": "fra",
        },
        "count": 120,
        "ok": 117,
        "cancelled": 1,
        "errors": {"overloaded": 2},
        "ttft_hist": [0, 0, 0, 0, 1, 3, 12, 40, 59, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        "duration_hist": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 9, 51, 43, 11, 1, 0, 0, 0, 0],
        "decode_hist": [0, 0, 0, 0, 1, 6, 48, 55, 7, 0, 0, 0, 0, 0],
        "input_tokens_sum": 512340,
        "output_tokens_sum": 48211,
        "output_tokens_estimated_count": 0,
    }
    record.update(changes)
    return record


def envelope(summaries: list[Any] | None = None, samples: list[Any] | None = None) -> Record:
    return {
        "schema_version": 1,
        "sdk": {"name": "gaslit-python", "version": "0.1.0"},
        "runtime": {"language": "python", "version": "3.12.4"},
        "sent_at": now_ms(),
        "dropped_since_last": 0,
        "summaries": summaries or [],
        "samples": samples or [],
    }


def gzipped(body: Record) -> bytes:
    return gzip.compress(json.dumps(body).encode(), mtime=0)
