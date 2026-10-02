from collections.abc import Iterator

import pytest

from yourpkg import _runtime
from yourpkg._config import REGION_ENV_VARS

KEY = "rm_live_testkey123"


@pytest.fixture(autouse=True)
def clean_sdk(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test starts with no SDK state and none of the variables the SDK reads."""
    for name in ("YOURPKG_KEY", "YOURPKG_ENDPOINT", "YOURPKG_ENABLED", "YOURPKG_DEBUG"):
        monkeypatch.delenv(name, raising=False)
    for name in REGION_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    _runtime.reset()
    yield
    _runtime.reset()
