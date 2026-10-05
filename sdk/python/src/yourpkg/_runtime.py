"""Process-wide SDK state: ``init()``, recording finished calls and lifecycle (brief §6.1, §6.7)."""

from __future__ import annotations

import atexit
import os
import threading
from typing import Any

from . import _log
from ._aggregator import Aggregator, Sample, Summary
from ._config import Config, is_enabled, load_config
from ._model import Call
from ._queue import BoundedQueue
from ._sender import Sender

EXIT_TIMEOUT_S = 2.0


class State:
    __slots__ = ("aggregator", "config", "queue", "sender")

    def __init__(self, config: Config) -> None:
        self.config = config
        self.aggregator = Aggregator(config.sample_rate, config.max_samples_per_minute)
        self.queue: BoundedQueue[Sample | Summary] = BoundedQueue(config.max_queue)
        self.sender = Sender(config, self.aggregator, self.queue, lambda: _disable(self))


_lock = threading.Lock()
_initialised = False
_hooks_registered = False
state: State | None = None  # None means every SDK entry point is a no-op


def init(enabled: object, **options: Any) -> None:
    global _initialised, state
    with _lock:
        if _initialised:
            _log.warn("init_twice", "yourpkg.init() was already called; ignoring this call.")
            return
        _initialised = True
        if not is_enabled(enabled, os.environ):
            return
        config = load_config(**options)
        _log.debug = config.debug
        # Without a key or endpoint, only debug mode has a use: printing payloads locally.
        if config.can_send or config.debug:
            state = State(config)
            _register_hooks()


def record(call: Call) -> None:
    """Tally a finished call and queue it if it was sampled. Runs on the caller's thread."""
    s = state
    if s is None:
        return
    s.sender.start()  # lazily, on the first call; a no-op afterwards
    reason = s.aggregator.add(call)
    if (
        reason is not None
        and s.queue.put(Sample(call, reason))
        and len(s.queue) == s.config.max_batch
    ):
        s.sender.wake()
    if s.aggregator.folded:
        _log.warn(
            "cardinality",
            "Over 500 series this minute; extra series are counted as model='other'. "
            "Check for dynamically generated model names.",
        )


def flush(timeout: float) -> None:
    s = state
    if s is not None:
        s.sender.flush(timeout)


def shutdown(timeout: float) -> None:
    """Flush, then stop. Calls made afterwards are not measured."""
    global state
    with _lock:
        s, state = state, None
    if s is not None:
        s.sender.stop(timeout)


def _disable(s: State) -> None:
    """Called by the sender when the key is rejected: stop measuring in this process."""
    global state
    with _lock:
        if state is s:
            state = None


def _after_fork_in_child() -> None:
    # The parent sends its own data; the child starts empty, with no sender thread yet.
    global _lock, state
    _lock = threading.Lock()  # may have been held by another thread at fork time
    if state is not None:
        state = State(state.config)


def _register_hooks() -> None:
    global _hooks_registered
    if _hooks_registered:
        return
    _hooks_registered = True
    atexit.register(shutdown, EXIT_TIMEOUT_S)
    if hasattr(os, "register_at_fork"):  # not on Windows
        os.register_at_fork(after_in_child=_after_fork_in_child)


def reset() -> None:
    """Forget all state and stop the sender thread. For tests."""
    global _initialised, state
    with _lock:
        _initialised = False
        s, state = state, None
        _log.reset()
    if s is not None:
        s.sender.stop(0)
