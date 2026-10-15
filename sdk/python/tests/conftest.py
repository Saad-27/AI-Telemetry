from collections.abc import Iterator

import pytest
from fake_ingest import FakeIngest

from gaslit import _openai, _runtime
from gaslit._config import REGION_ENV_VARS
from gaslit._sender import Sender

KEY = "gl_live_testkey123"


@pytest.fixture(autouse=True)
def clean_sdk(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test starts with no SDK state and none of the variables the SDK reads.

    The background sender stays off unless the test is marked ``sender``, so unit tests
    can inspect the aggregator and queue without racing it.
    """
    if request.node.get_closest_marker("sender") is None:
        monkeypatch.setattr(Sender, "start", lambda self: None)
    for name in ("GASLIT_KEY", "GASLIT_ENDPOINT", "GASLIT_ENABLED", "GASLIT_DEBUG"):
        monkeypatch.delenv(name, raising=False)
    for name in REGION_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    _runtime.reset()
    yield
    _runtime.reset()
    _openai.uninstall()  # init() patches the openai library; leave it as found


@pytest.fixture
def ingest(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeIngest]:
    """A running fake ingest, with the sender's retry delays and timeout shortened."""
    monkeypatch.setattr("gaslit._sender.RETRY_DELAYS_S", (0.01, 0.01, 0.01))
    monkeypatch.setattr("gaslit._sender.TIMEOUT_S", 0.5)
    with FakeIngest() as fake:
        yield fake
