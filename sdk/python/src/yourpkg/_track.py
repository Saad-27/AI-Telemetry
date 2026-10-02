"""Manual instrumentation: ``yourpkg.track()`` (brief §6.1, ADR 0003).

Every method catches its own failures (P4). ``__exit__`` never suppresses the user's
exception, so it propagates unchanged.
"""

from __future__ import annotations

from types import TracebackType

from . import _log, _runtime
from ._model import (
    CANCELLED,
    ERROR,
    MAX_TOKENS,
    PROVIDERS,
    ROUTES,
    Call,
    classify_http,
    valid_error_code,
    valid_model,
    valid_tag,
)


def _count(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_TOKENS:
        return value
    return None


def _http_status(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
        return value
    return None


def _exception_class(exc: BaseException) -> str:
    # Classified by type only. Message text is never read (P1).
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ConnectionError):
        return "connection_error"
    return "unknown"


class Tracker:
    """Returned by ``yourpkg.track()``. A no-op when the SDK is off."""

    __slots__ = ("_call",)

    def __init__(self, call: Call | None) -> None:
        self._call = call

    def __enter__(self) -> Tracker:
        return self

    def first_token(self) -> None:
        """Mark the first output chunk. Cheap to call on every chunk; only the first counts."""
        call = self._call
        if call is not None and call.ttft_ms is None:
            try:  # noqa: SIM105 (per-chunk path: cheaper than contextlib.suppress)
                call.first_token()
            except Exception:  # noqa: S110 (fail open, P4)
                pass

    def set_usage(
        self,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cached_input_tokens: int | None = None,
    ) -> None:
        call = self._call
        if call is None:
            return
        try:
            call.input_tokens = _count(input_tokens)
            call.output_tokens = _count(output_tokens)
            call.cached_input_tokens = _count(cached_input_tokens)
        except Exception:  # noqa: S110 (fail open, P4)
            pass

    def set_error(self, http_status: int | None = None, code: str | None = None) -> None:
        """Record a failed call without raising, e.g. a raw HTTP response with status 503."""
        call = self._call
        if call is None:
            return
        try:
            status = _http_status(http_status)
            short = code if isinstance(code, str) and valid_error_code(code) else None
            call.status = ERROR
            call.http_status = status
            call.provider_error_code = short
            call.error_class = classify_http(status, short)
        except Exception:  # noqa: S110 (fail open, P4)
            pass

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        call, self._call = self._call, None
        if call is None:
            return
        try:
            call.finish()
            if exc is not None:
                if not isinstance(exc, Exception):
                    # CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit.
                    call.status = call.error_class = CANCELLED
                elif call.status != ERROR:  # details from set_error() win
                    call.status = ERROR
                    call.error_class = _exception_class(exc)
            _runtime.record(call)
        except Exception:  # noqa: S110 (fail open, P4)
            pass


def track(
    provider: str,
    model: str,
    *,
    label: str | None = None,
    streaming: bool = False,
    route: str | None = None,
) -> Tracker:
    if _runtime.state is None:
        return Tracker(None)
    try:
        if provider not in PROVIDERS:
            _log.warn("bad_provider", "Unknown provider passed to track(); using 'custom'.")
            provider = "custom"
        if route is None:
            route = "local" if provider == "local" else "custom"
        elif route not in ROUTES:
            _log.warn("bad_route", "Unknown route passed to track(); using 'custom'.")
            route = "custom"
        if not (isinstance(model, str) and valid_model(model)):
            _log.warn("bad_model", "Invalid model name passed to track(); using 'invalid'.")
            model = "invalid"
        if label is not None and not (isinstance(label, str) and valid_tag(label)):
            _log.warn("bad_label", "Invalid label passed to track(); ignoring it.")
            label = None
        return Tracker(
            Call(
                provider=provider,
                route=route,
                operation="manual",
                model_requested=model,
                streaming=streaming is True,
                label=label,
            )
        )
    except Exception:
        return Tracker(None)
