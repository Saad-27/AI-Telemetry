"""Process-wide SDK state: ``init()`` and recording finished calls (brief §6.1, §6.7)."""

from __future__ import annotations

import os
import threading
from typing import Any

from . import _log
from ._aggregator import Aggregator, Sample, Summary
from ._config import Config, is_enabled, load_config
from ._model import Call
from ._queue import BoundedQueue


class State:
    __slots__ = ("aggregator", "config", "queue")

    def __init__(self, config: Config) -> None:
        self.config = config
        self.aggregator = Aggregator(config.sample_rate, config.max_samples_per_minute)
        self.queue: BoundedQueue[Sample | Summary] = BoundedQueue(config.max_queue)


_lock = threading.Lock()
_initialised = False
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


def record(call: Call) -> None:
    """Tally a finished call and queue it if it was sampled. Runs on the caller's thread."""
    s = state
    if s is None:
        return
    reason = s.aggregator.add(call)
    if reason is not None:
        s.queue.put(Sample(call, reason))
    if s.aggregator.folded:
        _log.warn(
            "cardinality",
            "Over 500 series this minute; extra series are counted as model='other'. "
            "Check for dynamically generated model names.",
        )


def reset() -> None:
    """Forget all state. For tests."""
    global _initialised, state
    with _lock:
        _initialised = False
        state = None
        _log.reset()
