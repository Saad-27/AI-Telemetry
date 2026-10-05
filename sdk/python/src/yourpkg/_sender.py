"""Background sender: batching, HTTP, retries and flushing (brief §6.7, §19.3, ADR 0004).

All network I/O happens on one daemon thread. The caller's thread only starts or wakes it.
"""

from __future__ import annotations

import gzip
import json
import random
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable

from . import __version__, _log
from ._aggregator import Aggregator, Sample, Summary
from ._config import Config
from ._model import SCHEMA_VERSION, sample_record
from ._queue import BoundedQueue

CLOSE_DELAY_MS = 2_000  # a minute is closed ~2 s after it ends
TIMEOUT_S = 5.0  # per socket operation, connect included
RETRY_DELAYS_S = (1.0, 2.0, 4.0)  # 5xx or network error; then the batch is dropped
RETRY_AFTER_DEFAULT_S = 30.0  # 429 without a usable Retry-After
RETRY_AFTER_MAX_S = 300.0
MIN_WAIT_S = 0.05

SDK = {"name": "yourpkg-python", "version": __version__}
RUNTIME = {"language": "python", "version": "{}.{}.{}".format(*sys.version_info[:3])}
USER_AGENT = f"yourpkg-python/{__version__}"

Record = dict[str, object]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would forward the Authorization header to another URL. Treat it as an error."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def _opener(url: str) -> urllib.request.OpenerDirector:
    # An explicit context keeps TLS verification on even if the app has patched
    # ssl._create_default_https_context. Plain http is only allowed to loopback, never proxied.
    handlers: list[urllib.request.BaseHandler] = [
        _NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    ]
    if url.startswith("http://"):
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def retry_after_s(value: str | None) -> float:
    """Seconds to wait after a 429. Only the delta-seconds form is understood."""
    try:
        seconds = float(value) if value is not None else RETRY_AFTER_DEFAULT_S
    except ValueError:
        seconds = RETRY_AFTER_DEFAULT_S
    if not 0 <= seconds <= RETRY_AFTER_MAX_S:  # also false for NaN
        seconds = RETRY_AFTER_MAX_S if seconds > RETRY_AFTER_MAX_S else RETRY_AFTER_DEFAULT_S
    return seconds


def _minute(ms: int) -> int:
    return ms - ms % 60_000


class Sender:
    def __init__(
        self,
        config: Config,
        aggregator: Aggregator,
        queue: BoundedQueue[Sample | Summary],
        on_unauthorised: Callable[[], None],
    ) -> None:
        self._config = config
        self._aggregator = aggregator
        self._queue = queue
        self._on_unauthorised = on_unauthorised
        self._cond = threading.Condition(threading.Lock())
        self._thread: threading.Thread | None = None
        self._woken = False
        self._stopping = False
        self._flush_requested = 0
        self._flush_done = 0
        # Owned by the sender thread only.
        self._opener: urllib.request.OpenerDirector | None = None
        self._pending: deque[tuple[list[Record], bool]] = deque()  # (records, may_split)
        self._attempt = 0
        self._not_before = 0.0  # monotonic time before which nothing is sent
        self._dropped = 0  # records dropped since the last accepted batch

    # Caller side: cheap, never does I/O.

    def start(self) -> None:
        if self._thread is not None:
            return
        with self._cond:
            if self._thread is None:
                thread = threading.Thread(target=self._run, name="yourpkg-sender", daemon=True)
                thread.start()
                self._thread = thread

    def wake(self) -> None:
        with self._cond:
            self._woken = True
            self._cond.notify_all()

    def flush(self, timeout: float) -> bool:
        """Send everything recorded so far, including the current minute. Waits up to
        ``timeout`` seconds for the sender thread; returns False if it didn't finish."""
        if self._thread is None:
            return True
        deadline = time.monotonic() + timeout
        with self._cond:
            self._flush_requested += 1
            target = self._flush_requested
            self._woken = True
            self._cond.notify_all()
            while self._flush_done < target:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
        return True

    def stop(self, timeout: float) -> None:
        try:
            self.flush(timeout)
        finally:
            with self._cond:
                self._stopping = True
                self._cond.notify_all()

    # Sender thread.

    def _run(self) -> None:
        while True:
            with self._cond:
                if not (self._woken or self._stopping):
                    self._cond.wait(self._wait_s())
                if self._stopping:
                    return
                self._woken = False
                flush_requested = self._flush_requested
            try:
                self._cycle(close_all=flush_requested > self._flush_done)
            except Exception:  # fail open (P4): an internal fault must not stop sending
                _log.warn("internal", "Internal error in the sender; continuing.")
            with self._cond:
                if self._stopping or not (self._pending or len(self._queue)):
                    self._flush_done = flush_requested
                    self._cond.notify_all()

    def _wait_s(self) -> float:
        now_ms = time.time_ns() // 1_000_000
        until_close_ms = 60_000 - (now_ms - CLOSE_DELAY_MS) % 60_000
        wait = min(self._config.flush_interval, until_close_ms / 1000)
        if self._pending:
            wait = min(wait, self._not_before - time.monotonic())
        return max(wait, MIN_WAIT_S)

    def _cycle(self, close_all: bool) -> None:
        if close_all:
            closed = self._aggregator.pop_all()
        else:
            closed = self._aggregator.pop_closed(
                _minute(time.time_ns() // 1_000_000 - CLOSE_DELAY_MS)
            )
        for summary in closed:
            self._queue.put(summary)
        while time.monotonic() >= self._not_before:
            if not self._pending:
                items = self._queue.take(self._config.max_batch)
                if not items:
                    return
                try:
                    self._pending.append((self._records(items), True))
                except Exception:
                    self._dropped += len(items)
                    raise
            try:
                carry_on = self._send(*self._pending[0])
            except Exception:
                # A batch that can't be encoded or sent must not block the ones behind it.
                self._drop()
                raise
            if not carry_on:
                return

    def _records(self, items: list[Sample | Summary]) -> list[Record]:
        # IDs are generated here, once, so retries of the batch are idempotent on the server.
        tags = self._config.tags
        return [
            item.record(tags)
            if isinstance(item, Summary)
            else sample_record(item.call, item.reason, tags)
            for item in items
        ]

    def _send(self, records: list[Record], may_split: bool) -> bool:
        """Send the batch at the head of ``_pending``. Returns False to end this cycle."""
        self._dropped += self._queue.take_dropped()
        body = json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "sdk": SDK,
                "runtime": RUNTIME,
                "sent_at": time.time_ns() // 1_000_000,
                "dropped_since_last": self._dropped,
                "summaries": [r for r in records if "summary_id" in r],
                "samples": [r for r in records if "event_id" in r],
            },
            separators=(",", ":"),
            allow_nan=False,
        )
        if self._config.debug:
            print(f"[yourpkg] payload {body}", file=sys.stderr)  # exactly what is sent (P5)
        if not self._config.can_send:  # debug without a key: print only
            self._accepted()
            return True

        status, retry_after = self._post(gzip.compress(body.encode(), mtime=0))
        if status is not None and 200 <= status < 300:
            self._accepted()
            return True
        if status in (401, 403):
            _log.warn(
                "unauthorised", f"The ingest key was rejected (HTTP {status}). Sending has stopped."
            )
            self._pending.clear()
            with self._cond:
                self._stopping = True
            self._on_unauthorised()
            return False
        if status == 429:
            self._not_before = time.monotonic() + retry_after_s(retry_after)
            return False
        if status == 413 and may_split and len(records) > 1:
            self._pending.popleft()
            mid = len(records) // 2
            self._pending.extendleft([(records[mid:], False), (records[:mid], False)])
            return True
        if status is None or status >= 500:
            if self._attempt < len(RETRY_DELAYS_S):
                delay = RETRY_DELAYS_S[self._attempt] * (0.5 + random.random())  # noqa: S311
                self._attempt += 1
                self._not_before = time.monotonic() + delay
            else:
                _log.warn("send_failed", "Could not reach the ingest API; dropped a batch.")
                self._drop()
            return False
        # 400, an unsplittable 413, a redirect or another 4xx: retrying can't help.
        _log.warn("rejected", f"The ingest API rejected a batch (HTTP {status}); dropped it.")
        self._drop()
        return True

    def _post(self, body: bytes) -> tuple[int | None, str | None]:
        """POST a gzipped batch. Returns the status (None on a network error) and Retry-After."""
        if self._opener is None:
            self._opener = _opener(self._config.ingest_url or "")
        request = urllib.request.Request(  # noqa: S310 (scheme checked in _config._endpoint)
            self._config.ingest_url or "",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._config.api_key}",
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with self._opener.open(request, timeout=TIMEOUT_S) as response:
                return response.status, None
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers.get("Retry-After")
        except Exception:  # DNS, TLS, refused, timeout, malformed response
            return None, None

    def _accepted(self) -> None:
        self._pending.popleft()
        self._attempt = 0
        self._dropped = 0

    def _drop(self) -> None:
        if not self._pending:
            return
        records, _ = self._pending.popleft()
        self._attempt = 0
        self._dropped += len(records)
