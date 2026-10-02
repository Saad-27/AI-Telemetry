"""Configuration from ``init()`` arguments and ``YOURPKG_*`` variables (brief §6.1, §11.7).

Invalid values never raise: each one is replaced by a safe default and warned about once.
Warnings never echo the value, since a bad key or endpoint may hold a secret.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from . import _log
from ._model import Tags, valid_tag

# Placeholder until the product name is chosen (D1). The .invalid TLD never resolves.
DEFAULT_ENDPOINT = "https://ingest.yourpkg.invalid"
INGEST_PATH = "/v1/ingest"
KEY_PREFIX = "rm_"
REGION_ENV_VARS = ("FLY_REGION", "AWS_REGION", "VERCEL_REGION")

_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
_FALSE = frozenset({"0", "false", "no", "off"})
_TRUE = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True, slots=True)
class Config:
    api_key: str | None = field(repr=False)
    ingest_url: str | None
    debug: bool
    flush_interval: float
    max_batch: int
    max_queue: int
    sample_rate: float
    max_samples_per_minute: int
    tags: Tags

    @property
    def can_send(self) -> bool:
        return self.api_key is not None and self.ingest_url is not None


def is_enabled(enabled: object, environ: Mapping[str, str]) -> bool:
    """``YOURPKG_ENABLED=false`` is a kill switch and wins over the argument."""
    if environ.get("YOURPKG_ENABLED", "").strip().lower() in _FALSE:
        return False
    return enabled is not False


def load_config(
    *,
    api_key: object = None,
    service: object = None,
    environment: object = None,
    endpoint: object = None,
    debug: object = False,
    flush_interval: object = 5.0,
    max_batch: object = 200,
    max_queue: object = 10_000,
    sample_rate: object = 0.05,
    max_samples_per_minute: object = 10,
    origin_region: object = None,
    environ: Mapping[str, str] = os.environ,
) -> Config:
    return Config(
        api_key=_api_key(api_key if api_key is not None else environ.get("YOURPKG_KEY")),
        ingest_url=_endpoint(endpoint if endpoint is not None else environ.get("YOURPKG_ENDPOINT")),
        debug=debug is True or environ.get("YOURPKG_DEBUG", "").strip().lower() in _TRUE,
        flush_interval=_number("flush_interval", flush_interval, 5.0, 0.1, 300.0),
        max_batch=int(_number("max_batch", max_batch, 200, 1, 1000, integer=True)),
        max_queue=int(_number("max_queue", max_queue, 10_000, 1, 1_000_000, integer=True)),
        sample_rate=_number("sample_rate", sample_rate, 0.05, 0.0, 1.0),
        max_samples_per_minute=int(
            _number("max_samples_per_minute", max_samples_per_minute, 10, 0, 1000, integer=True)
        ),
        tags=Tags(
            service=_tag("service", service),
            environment=_tag("environment", environment),
            origin_region=_tag("origin_region", _region(origin_region, environ)),
        ),
    )


def _api_key(key: object) -> str | None:
    key = key.strip() if isinstance(key, str) else None
    if not key:
        _log.warn("no_key", "No ingest key: set YOURPKG_KEY or pass api_key. Nothing will be sent.")
        return None
    if not key.startswith(KEY_PREFIX) or not (key.isascii() and key.replace("_", "").isalnum()):
        # Also stops a provider key pasted into YOURPKG_KEY from ever reaching our ingest.
        _log.warn(
            "bad_key",
            f"The ingest key is malformed (expected {KEY_PREFIX}...). Nothing will be sent.",
        )
        return None
    return key


def _endpoint(endpoint: object) -> str | None:
    """Full ingest URL, or None. HTTPS only, except plain HTTP to loopback for local dev."""
    if endpoint is None:
        endpoint = DEFAULT_ENDPOINT
    url = None
    if isinstance(endpoint, str):
        try:
            u = urlsplit(endpoint.strip())
            u.port  # noqa: B018 (raises ValueError on a malformed port)
            host = u.hostname
            if (
                host
                and not (u.query or u.fragment or u.username or u.password)
                and (u.scheme == "https" or (u.scheme == "http" and host in _LOOPBACK))
            ):
                url = urlunsplit((u.scheme, u.netloc, u.path.rstrip("/") + INGEST_PATH, "", ""))
        except ValueError:
            pass
    if url is None:
        _log.warn(
            "bad_endpoint",
            "The endpoint must be an https:// URL with no query (http:// only for localhost). "
            "Nothing will be sent.",
        )
    return url


def _region(origin_region: object, environ: Mapping[str, str]) -> object:
    if origin_region is not None:
        return origin_region
    for name in REGION_ENV_VARS:
        value = environ.get(name, "").strip()
        if value:
            return value
    return None


def _tag(name: str, value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and valid_tag(value):
        return value
    _log.warn(f"bad_{name}", f"Ignoring {name}: use 1-64 characters from A-Z a-z 0-9 . _ : -")
    return None


def _number(
    name: str, value: object, default: float, lo: float, hi: float, integer: bool = False
) -> float:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and not (integer and isinstance(value, float))
        and math.isfinite(value)
        and lo <= value <= hi
    ):
        return value
    _log.warn(f"bad_{name}", f"Ignoring {name}: expected a number from {lo} to {hi}")
    return default
