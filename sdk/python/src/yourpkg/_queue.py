"""Bounded in-memory queue between the call path and the sender (brief §6.3, §6.7)."""

from __future__ import annotations

import threading
from collections import deque
from typing import Generic, TypeVar

T = TypeVar("T")


class BoundedQueue(Generic[T]):
    """Never blocks. When full, the newest item is dropped and counted."""

    def __init__(self, maxlen: int) -> None:
        self._maxlen = maxlen
        self._items: deque[T] = deque()
        self._lock = threading.Lock()
        self._dropped = 0

    def __len__(self) -> int:
        return len(self._items)

    def put(self, item: T) -> bool:
        with self._lock:
            if len(self._items) >= self._maxlen:
                self._dropped += 1
                return False
            self._items.append(item)
            return True

    def take(self, n: int) -> list[T]:
        """Remove and return up to ``n`` items, oldest first."""
        with self._lock:
            return [self._items.popleft() for _ in range(min(n, len(self._items)))]

    def take_dropped(self) -> int:
        """Return the number of items dropped since the last call, and reset it."""
        with self._lock:
            dropped, self._dropped = self._dropped, 0
            return dropped
