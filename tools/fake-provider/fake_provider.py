"""A local stand-in for the OpenAI and Anthropic APIs (brief §9.11), for tests and demos.

It answers ``/v1/chat/completions``, ``/v1/responses`` and ``/v1/messages``, streaming or not,
with the real response shapes, so the official client libraries work against it unchanged.
Each reply follows a ``Behavior``: latency, a slow stream, 429, 529, 5xx or a hang long enough
to time the client out. No tokens are spent.

Local use::

    python tools/fake-provider/fake_provider.py 8766 latency_ms=300 chunk_ms=50
    OPENAI_BASE_URL=http://127.0.0.1:8766/v1 OPENAI_API_KEY=sk-fake python my_app.py

The same ``key=value`` pairs work per request in an ``x-fake-behavior`` header, for example
``x-fake-behavior: status=429,retry_after_s=2``. Tests script replies with ``FakeProvider.script``.

Only the standard library is used. The server keeps no request bodies: it records the path,
model and stream flag of each call, nothing else.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

HEADER = "x-fake-behavior"


@dataclass
class Behavior:
    status: int = 200
    # Non-streaming: the wait before the reply (or error). Streaming: the time to first token.
    # A value above the client's timeout simulates a timeout.
    latency_ms: int = 0
    chunk_ms: int = 0  # Streaming: the gap between tokens, for a slow stream.
    chunks: int = 5  # The reply is this many tokens long, so it reports this many output tokens.
    input_tokens: int = 10
    retry_after_s: int = 0  # Sent as Retry-After on an error status when above 0.

    @classmethod
    def parse(cls, text: str) -> Behavior:
        """Parse ``status=429,latency_ms=200``. Raises ValueError on a bad name or value."""
        known = {f.name for f in fields(cls)}
        values: dict[str, int] = {}
        for pair in text.replace(" ", ",").split(","):
            if not pair:
                continue
            name, _, value = pair.partition("=")
            if name not in known:
                raise ValueError(f"unknown behavior {name!r}; known: {sorted(known)}")
            values[name] = int(value)
        return cls(**values)


@dataclass
class Call:
    path: str
    model: str
    stream: bool
    status: int


ERROR_TYPES = {  # status: (Anthropic error type, OpenAI error type, OpenAI error code)
    400: ("invalid_request_error", "invalid_request_error", "invalid_request"),
    401: ("authentication_error", "invalid_request_error", "invalid_api_key"),
    429: ("rate_limit_error", "rate_limit_error", "rate_limit_exceeded"),
    529: ("overloaded_error", "server_error", "overloaded"),
}
ROUTES = {
    "/v1/chat/completions": "chat",
    "/v1/responses": "responses",
    "/v1/messages": "messages",
}


def token(i: int) -> str:
    return f"word{i} "


def error_body(route: str, status: int) -> dict[str, Any]:
    anthropic, openai, code = ERROR_TYPES.get(status, ("api_error", "server_error", "server_error"))
    message = f"Fake provider error {status}"
    if route == "messages":
        return {"type": "error", "error": {"type": anthropic, "message": message}}
    return {"error": {"message": message, "type": openai, "param": None, "code": code}}


def message_body(route: str, model: str, b: Behavior) -> dict[str, Any]:
    text = "".join(token(i) for i in range(b.chunks))
    now = int(time.time())
    usage = {"input_tokens": b.input_tokens, "output_tokens": b.chunks}
    if route == "messages":
        return {
            "id": "msg_fake", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
            "stop_sequence": None, "usage": usage,
        }  # fmt: skip
    if route == "responses":
        return {
            "id": "resp_fake", "object": "response", "created_at": now, "status": "completed",
            "model": model, "error": None, "incomplete_details": None, "tools": [],
            "tool_choice": "auto", "parallel_tool_calls": True,
            "output": [{
                "type": "message", "id": "msg_fake", "status": "completed", "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }],
            "usage": {
                **usage, "total_tokens": b.input_tokens + b.chunks,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        }  # fmt: skip
    return {
        "id": "chatcmpl-fake", "object": "chat.completion", "created": now, "model": model,
        "choices": [{
            "index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": text, "refusal": None},
        }],
        "usage": {
            "prompt_tokens": b.input_tokens, "completion_tokens": b.chunks,
            "total_tokens": b.input_tokens + b.chunks,
        },
    }  # fmt: skip


Event = tuple[str, dict[str, Any]]  # (SSE event name, data)


def stream_events(route: str, model: str, b: Behavior, include_usage: bool) -> list[list[Event]]:
    """The reply as groups of events. The first group is sent at once, then the second after
    ``latency_ms`` (the first token), then each later group after ``chunk_ms``."""
    now = int(time.time())
    if route == "messages":
        usage = {"input_tokens": b.input_tokens, "output_tokens": 1}
        start = {
            "type": "message_start",
            "message": {
                "id": "msg_fake", "type": "message", "role": "assistant", "model": model,
                "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage,
            },
        }  # fmt: skip
        block = {"type": "content_block_start", "index": 0,
                 "content_block": {"type": "text", "text": ""}}  # fmt: skip
        groups: list[list[Event]] = [[("message_start", start), ("content_block_start", block)]]
        groups += [
            [("content_block_delta", {"type": "content_block_delta", "index": 0,
                                      "delta": {"type": "text_delta", "text": token(i)}})]
            for i in range(b.chunks)
        ]  # fmt: skip
        groups.append([
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta",
                               "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                               "usage": {"output_tokens": b.chunks}}),
            ("message_stop", {"type": "message_stop"}),
        ])  # fmt: skip
        return groups
    if route == "responses":
        done = message_body(route, model, b)
        shell = {**done, "status": "in_progress", "output": [], "usage": None}
        item = {"type": "message", "id": "msg_fake", "status": "in_progress",
                "role": "assistant", "content": []}  # fmt: skip
        part = {"type": "output_text", "text": "", "annotations": []}
        groups = [[("response.created", {"type": "response.created", "response": shell}),
                   ("response.in_progress", {"type": "response.in_progress", "response": shell}),
                   ("response.output_item.added",
                    {"type": "response.output_item.added", "output_index": 0, "item": item}),
                   ("response.content_part.added",
                    {"type": "response.content_part.added", "output_index": 0,
                     "item_id": "msg_fake", "content_index": 0, "part": part})]]  # fmt: skip
        groups += [
            [("response.output_text.delta",
              {"type": "response.output_text.delta", "output_index": 0, "item_id": "msg_fake",
               "content_index": 0, "delta": token(i)})]
            for i in range(b.chunks)
        ]  # fmt: skip
        text = done["output"][0]["content"][0]["text"]
        groups.append([
            ("response.output_text.done",
             {"type": "response.output_text.done", "output_index": 0, "item_id": "msg_fake",
              "content_index": 0, "text": text}),
            ("response.output_item.done",
             {"type": "response.output_item.done", "output_index": 0, "item": done["output"][0]}),
            ("response.completed", {"type": "response.completed", "response": done}),
        ])  # fmt: skip
        for n, (_, data) in enumerate(e for g in groups for e in g):
            data["sequence_number"] = n
        return groups

    def chunk(delta: dict[str, Any], finish: str | None) -> Event:
        return ("", {
            "id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": now,
            "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        })  # fmt: skip

    groups = [[chunk({"role": "assistant", "content": ""}, None)]]
    groups += [[chunk({"content": token(i)}, None)] for i in range(b.chunks)]
    last = [chunk({}, "stop")]
    if include_usage:
        last.append(("", {
            "id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": now,
            "model": model, "choices": [],
            "usage": {"prompt_tokens": b.input_tokens, "completion_tokens": b.chunks,
                      "total_tokens": b.input_tokens + b.chunks},
        }))  # fmt: skip
    groups.append(last)
    return groups


def sse(event: Event) -> bytes:
    name, data = event
    head = f"event: {name}\n" if name else ""
    return f"{head}data: {json.dumps(data)}\n\n".encode()


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, *_: object) -> None:
        pass  # e.g. the client timed out and closed the connection


class FakeProvider:
    def __init__(self, port: int = 0, default: Behavior | None = None) -> None:
        self.default = default or Behavior()
        self.calls: list[Call] = []
        self.replies: deque[Behavior] = deque()  # scripted; ``default`` once empty
        self._cond = threading.Condition()
        self._server = _QuietServer(("127.0.0.1", port), self._handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, args=(0.05,), daemon=True
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> FakeProvider:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def script(self, *behaviors: Behavior | int) -> None:
        """Queue replies for the next calls, in order. An int is a bare status."""
        self.replies.extend(b if isinstance(b, Behavior) else Behavior(status=b) for b in behaviors)

    def wait_for(self, n: int, timeout: float = 5.0) -> list[Call]:
        """Wait until ``n`` calls have been answered; return all of them."""
        with self._cond:
            self._cond.wait_for(lambda: len(self.calls) >= n, timeout)
            return list(self.calls)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                route = ROUTES.get(self.path.split("?")[0])
                try:
                    body = json.loads(raw)
                    behavior = (
                        Behavior.parse(self.headers[HEADER])
                        if HEADER in self.headers
                        else fake.replies.popleft()
                        if fake.replies
                        else fake.default
                    )
                except (ValueError, TypeError) as e:
                    return self._send(400, {"error": {"message": f"bad request: {e}"}})
                if route is None or not isinstance(body, dict):
                    return self._send(404, {"error": {"message": "unknown route"}})
                model = str(body.get("model", "fake-model"))
                stream = body.get("stream") is True
                options = body.get("stream_options")
                usage = isinstance(options, dict) and options.get("include_usage") is True
                time.sleep(behavior.latency_ms / 1000 if not (stream and behavior.status < 400)
                           else 0)  # fmt: skip
                try:
                    if behavior.status >= 400:
                        headers = {}
                        if behavior.retry_after_s:
                            headers["Retry-After"] = str(behavior.retry_after_s)
                        self._send(behavior.status, error_body(route, behavior.status), headers)
                    elif stream:
                        self._stream(route, model, behavior, usage)
                    else:
                        self._send(200, message_body(route, model, behavior))
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client gave up; a cancelled stream
                with fake._cond:
                    fake.calls.append(Call(self.path, model, stream, behavior.status))
                    fake._cond.notify_all()

            def _send(
                self, status: int, body: dict[str, Any], headers: dict[str, str] | None = None
            ) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            def _stream(self, route: str, model: str, b: Behavior, include_usage: bool) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                groups = stream_events(route, model, b, include_usage)
                for n, group in enumerate(groups):
                    if n:  # the first group goes out at once
                        time.sleep((b.latency_ms if n == 1 else b.chunk_ms) / 1000)
                    self.wfile.write(b"".join(sse(e) for e in group))
                    self.wfile.flush()
                if route == "chat":
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()

            def log_message(self, *_: object) -> None:
                pass

        return Handler


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8766
    pairs = [a for a in sys.argv[1:] if "=" in a]
    with FakeProvider(port, Behavior.parse(",".join(pairs))) as fake:
        print(f"Fake provider on {fake.url}/v1 (Ctrl-C to stop)")
        print(f"  OpenAI:    OPENAI_BASE_URL={fake.url}/v1  OPENAI_API_KEY=sk-fake")
        print(f"  Anthropic: ANTHROPIC_BASE_URL={fake.url}  ANTHROPIC_API_KEY=sk-ant-fake")
        seen = 0
        try:
            while True:
                for c in fake.wait_for(seen + 1, timeout=1.0)[seen:]:
                    seen += 1
                    print(f"{c.status} {c.path} model={c.model} stream={c.stream}")
        except KeyboardInterrupt:
            pass
