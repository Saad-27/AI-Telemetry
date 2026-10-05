import logging

import pytest
from conftest import KEY

from yourpkg._config import DEFAULT_ENDPOINT, Config, is_enabled, load_config
from yourpkg._model import Tags


def test_defaults() -> None:
    c = load_config(api_key=KEY, environ={})
    assert c.ingest_url == DEFAULT_ENDPOINT + "/v1/ingest"
    assert (c.flush_interval, c.max_batch, c.max_queue) == (5.0, 200, 10_000)
    assert (c.sample_rate, c.max_samples_per_minute) == (0.05, 10)
    assert c.tags == Tags()
    assert not c.debug
    assert c.can_send


def test_key_from_env_and_argument_wins() -> None:
    assert load_config(environ={"YOURPKG_KEY": KEY}).api_key == KEY
    assert load_config(api_key="rm_live_other", environ={"YOURPKG_KEY": KEY}).api_key == (
        "rm_live_other"
    )


@pytest.mark.parametrize("key", [None, "", "   ", "sk-SENTINEL-KEY", "rm_live_a\r\nX: y", 123])
def test_missing_or_malformed_key_disables_sending(key: object) -> None:
    c = load_config(api_key=key, environ={})
    assert c.api_key is None
    assert not c.can_send


def test_repr_never_shows_key() -> None:
    assert KEY not in repr(load_config(api_key=KEY, environ={}))


@pytest.mark.parametrize(
    ("endpoint", "url"),
    [
        ("https://ingest.example.com", "https://ingest.example.com/v1/ingest"),
        ("https://example.com/prefix/", "https://example.com/prefix/v1/ingest"),
        ("http://localhost:8000", "http://localhost:8000/v1/ingest"),
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000/v1/ingest"),
        ("http://[::1]:8000", "http://[::1]:8000/v1/ingest"),
        ("http://ingest.example.com", None),
        ("https://ingest.example.com/?key=SENTINEL_QS", None),
        ("https://user:pw@ingest.example.com", None),
        ("https://ingest.example.com:99999", None),
        ("ftp://ingest.example.com", None),
        ("not a url", None),
        (42, None),
    ],
)
def test_endpoint_rules(endpoint: object, url: str | None) -> None:
    assert load_config(api_key=KEY, endpoint=endpoint, environ={}).ingest_url == url


def test_endpoint_from_env() -> None:
    c = load_config(api_key=KEY, environ={"YOURPKG_ENDPOINT": "http://localhost:9"})
    assert c.ingest_url == "http://localhost:9/v1/ingest"


@pytest.mark.parametrize(
    ("arg", "env", "enabled"),
    [
        (True, None, True),
        (False, None, False),
        (True, "false", False),
        (True, " OFF ", False),
        (True, "0", False),
        (False, "true", False),  # the env var can only switch it off
        (True, "true", True),
    ],
)
def test_kill_switch(arg: bool, env: str | None, enabled: bool) -> None:
    environ = {} if env is None else {"YOURPKG_ENABLED": env}
    assert is_enabled(arg, environ) is enabled


def test_debug_from_argument_or_env() -> None:
    assert load_config(debug=True, environ={}).debug
    assert load_config(environ={"YOURPKG_DEBUG": "1"}).debug
    assert not load_config(environ={"YOURPKG_DEBUG": "no"}).debug


def test_tags_validated() -> None:
    c = load_config(service="summariser", environment="bad env!", environ={})
    assert c.tags == Tags(service="summariser", environment=None)


def test_region_argument_then_env_order() -> None:
    env = {"AWS_REGION": "eu-west-1", "FLY_REGION": "fra"}
    assert load_config(environ=env).tags.origin_region == "fra"
    assert load_config(environ={"AWS_REGION": "eu-west-1"}).tags.origin_region == "eu-west-1"
    assert load_config(origin_region="lhr", environ=env).tags.origin_region == "lhr"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("flush_interval", 0),
        ("flush_interval", float("nan")),
        ("max_batch", 5000),
        ("max_batch", 1.5),
        ("max_queue", True),
        ("sample_rate", 1.5),
        ("sample_rate", "0.1"),
        ("max_samples_per_minute", -1),
    ],
)
def test_bad_numbers_fall_back_to_defaults(name: str, value: object) -> None:
    good = load_config(environ={})
    bad = load_config(environ={}, **{name: value})
    assert getattr(bad, name) == getattr(good, name)


def test_warnings_never_echo_secrets(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="yourpkg")
    load_config(
        api_key="sk-SENTINEL-KEY",
        endpoint="https://sentinelcorp.example.com/?key=SENTINEL_QS",
        environ={},
    )
    assert caplog.records
    text = caplog.text
    for sentinel in ("SENTINEL", "sentinelcorp"):
        assert sentinel not in text


def test_config_is_frozen() -> None:
    c = load_config(environ={})
    with pytest.raises(AttributeError):
        c.debug = True  # type: ignore[misc]
    assert isinstance(c, Config)
