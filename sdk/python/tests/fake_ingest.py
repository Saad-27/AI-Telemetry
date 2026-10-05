"""A local stand-in for the ingest API (brief §9.2), for tests and local development.

It applies the real API's size limits and rejects unknown fields like the server's strict
schema (P1), so an SDK bug that adds a field fails here first. Responses can be scripted.

Local use::

    uv run python tests/fake_ingest.py 8765
    YOURPKG_ENDPOINT=http://127.0.0.1:8765 YOURPKG_KEY=rm_live_dev python my_app.py
"""

from __future__ import annotations

import gzip
import io
import json
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MAX_BODY = 1_000_000
MAX_DECOMPRESSED = 5_000_000
MAX_RECORDS = 1000

ENVELOPE_KEYS = {
    "schema_version", "sdk", "runtime", "sent_at", "dropped_since_last", "summaries", "samples",
}  # fmt: skip
SUMMARY_KEYS = {
    "summary_id", "minute", "series", "count", "ok", "cancelled", "errors", "ttft_hist",
    "duration_hist", "decode_hist", "input_tokens_sum", "output_tokens_sum",
    "output_tokens_estimated_count",
}  # fmt: skip
SERIES_KEYS = {
    "provider", "route", "endpoint_host", "endpoint_region", "operation", "model", "streaming",
    "input_bucket", "service", "environment", "origin_region",
}  # fmt: skip
SAMPLE_KEYS = {
    "event_id", "reason", "ts", "duration_ms", "ttft_ms", "provider", "route", "endpoint_host",
    "endpoint_region", "operation", "model_requested", "model_returned", "streaming", "has_tools",
    "input_tokens", "output_tokens", "cached_input_tokens", "output_tokens_estimated", "status",
    "error_class", "http_status", "provider_error_code", "service", "environment", "label",
    "origin_region", "provider_sdk", "provider_sdk_version",
}  # fmt: skip

GARBAGE = -1  # scripted "status": reply with bytes that aren't HTTP


@dataclass
class Reply:
    status: int = 202
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0


@dataclass
class Received:
    headers: dict[str, str]
    body: bytes  # decompressed
    envelope: dict[str, Any] | None  # None if the request was rejected
    status: int


def check_envelope(env: object) -> str | None:
    """Return why the envelope is malformed, or None."""
    if not isinstance(env, dict) or set(env) != ENVELOPE_KEYS:
        return "envelope keys"
    summaries, samples = env["summaries"], env["samples"]
    if not isinstance(summaries, list) or not isinstance(samples, list):
        return "record lists"
    if len(summaries) + len(samples) > MAX_RECORDS:
        return "too many records"
    for s in summaries:
        if not isinstance(s, dict) or set(s) != SUMMARY_KEYS or set(s["series"]) != SERIES_KEYS:
            return "summary keys"
    for s in samples:
        if not isinstance(s, dict) or set(s) != SAMPLE_KEYS:
            return "sample keys"
    return None


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, *_: object) -> None:
        pass  # e.g. the SDK timed out and closed the connection


class FakeIngest:
    def __init__(self, port: int = 0) -> None:
        self.received: list[Received] = []
        self.replies: deque[Reply] = deque()  # scripted; 202 once empty
        self._cond = threading.Condition()
        self._server = _QuietServer(("127.0.0.1", port), self._handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, args=(0.05,), daemon=True
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> FakeIngest:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def script(self, *replies: Reply | int) -> None:
        self.replies.extend(r if isinstance(r, Reply) else Reply(r) for r in replies)

    def wait_for(self, n: int, timeout: float = 5.0) -> list[Received]:
        """Wait until ``n`` requests have arrived; return all of them."""
        with self._cond:
            self._cond.wait_for(lambda: len(self.received) >= n, timeout)
            return list(self.received)

    def accepted(self) -> list[dict[str, Any]]:
        return [r.envelope for r in self.received if r.envelope is not None and r.status == 202]

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                reply = fake.replies.popleft() if fake.replies else Reply()
                status, body, env = self._check()
                if status == 202:
                    status = reply.status
                with fake._cond:
                    fake.received.append(Received(dict(self.headers), body, env, status))
                    fake._cond.notify_all()
                time.sleep(reply.delay)
                if status == GARBAGE:
                    self.wfile.write(b"\x00\x01 not http\r\n\r\n")
                    self.close_connection = True
                    return
                self.send_response(status)
                for name, value in reply.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _check(self) -> tuple[int, bytes, dict[str, Any] | None]:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                if self.path != "/v1/ingest":
                    return 404, b"", None
                if not self.headers.get("Authorization", "").startswith("Bearer rm_"):
                    return 401, b"", None
                if length > MAX_BODY:
                    return 413, b"", None
                try:
                    if self.headers.get("Content-Encoding") == "gzip":
                        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as f:
                            raw = f.read(MAX_DECOMPRESSED + 1)
                    if len(raw) > MAX_DECOMPRESSED:
                        return 413, b"", None
                    env = json.loads(raw)
                except (OSError, ValueError):
                    return 400, raw, None
                if check_envelope(env) is not None:
                    return 400, raw, None
                return 202, raw, env

            def log_message(self, *_: object) -> None:
                pass

        return Handler


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    with FakeIngest(port) as fake:
        print(f"Fake ingest on {fake.url}/v1/ingest (Ctrl-C to stop)")
        seen = 0
        try:
            while True:
                for r in fake.wait_for(seen + 1, timeout=1.0)[seen:]:
                    seen += 1
                    env = r.envelope or {}
                    print(
                        f"{r.status}: {len(env.get('summaries', []))} summaries, "
                        f"{len(env.get('samples', []))} samples, "
                        f"dropped_since_last={env.get('dropped_since_last')}"
                    )
        except KeyboardInterrupt:
            pass
