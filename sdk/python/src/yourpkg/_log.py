"""SDK logging (brief §6.7). Silent unless the app configures logging or ``debug=True``."""

from __future__ import annotations

import logging
import sys
import time

logger = logging.getLogger("yourpkg")
logger.addHandler(logging.NullHandler())

WARN_INTERVAL_S = 600.0
debug = False  # set by init(); also prints warnings to stderr
_last_warned: dict[str, float] = {}


def warn(kind: str, message: str) -> None:
    """Warn at most once per ``kind`` per 10 minutes. Messages must never contain keys,
    URLs or anything taken from the user's calls."""
    now = time.monotonic()
    last = _last_warned.get(kind)
    if last is not None and now - last < WARN_INTERVAL_S:
        return
    _last_warned[kind] = now
    logger.warning(message)
    if debug:
        print(f"[yourpkg] {message}", file=sys.stderr)


def reset() -> None:
    global debug
    debug = False
    _last_warned.clear()
