"""Setup shared by the examples. None of this is needed in a real app.

``offline()`` starts the fake provider (tools/fake-provider) on this machine and routes the
examples' plain-HTTP requests through it, as an HTTP proxy. The clients keep the real
provider hostnames in their URLs, which is all Gaslit reads, so the printed events look
like real ones, but nothing leaves the machine and no tokens are spent.
"""

from __future__ import annotations

import os
import sys
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "fake-provider"))

from fake_provider import Behavior, FakeProvider

__all__ = ["Behavior", "FakeProvider", "offline"]

_PROXY_VARS = ("HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy")


@contextmanager
def offline() -> Iterator[FakeProvider]:
    saved = {name: os.environ.get(name) for name in _PROXY_VARS}
    with FakeProvider() as fake:
        os.environ.update(HTTP_PROXY=fake.url, http_proxy=fake.url, NO_PROXY="", no_proxy="")
        # httpx reads the proxy from here; refuse to run rather than reach a real provider.
        if urllib.request.getproxies().get("http") != fake.url:
            raise SystemExit("Could not route requests to the fake provider; not running.")
        try:
            yield fake
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
