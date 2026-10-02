import logging

import pytest

from yourpkg import _log


def test_warnings_rate_limited_per_kind(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.WARNING, logger="yourpkg")
    now = [1000.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    _log.warn("a", "first")
    _log.warn("a", "again")
    _log.warn("b", "other kind")
    now[0] += _log.WARN_INTERVAL_S
    _log.warn("a", "after interval")
    assert [r.message for r in caplog.records] == ["first", "other kind", "after interval"]


def test_silent_on_stderr_unless_debug(capsys: pytest.CaptureFixture[str]) -> None:
    _log.warn("quiet", "not printed")
    assert capsys.readouterr().err == ""
    _log.debug = True
    _log.warn("loud", "printed")
    assert capsys.readouterr().err == "[yourpkg] printed\n"
