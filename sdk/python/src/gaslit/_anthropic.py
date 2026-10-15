"""Anthropic integration: measures ``messages.create`` and the ``messages.stream()`` helper
(brief §6.2, §6.3, ADR 0011).

The ``Messages`` resource classes are patched, so clients made before ``init()`` are covered,
and so are the Bedrock, Vertex and Foundry clients, which use the same classes. ``stream()``
doesn't go through ``create``: it returns a context manager that sends the request on
``__enter__``, so the manager's enter is wrapped too, and the raw stream inside the
``MessageStream`` it yields is measured. Arguments, results and exceptions pass through
untouched.

Read from a call: the model name, whether ``stream`` and ``tools`` were given (a bool, not the
tools), the client's hostname, the usage counts, the returned model, the exception type, the
HTTP status and the short error type. Never read: messages, system prompts, outputs, thinking,
tool data, keys, headers, URL paths or queries, or error message text (P1).

Every entry point catches its own failures, so nothing here can raise into user code (P4).
"""

from __future__ import annotations

import contextlib
import functools
import importlib
import importlib.util
import time
from typing import Any

from . import _log, _runtime, _wrap
from ._hosts import detect
from ._model import CANCELLED, ERROR, Call, _now_ms, classify_http, valid_error_code, valid_model
from ._track import _count, _http_status

_RESOURCES = "anthropic.resources.messages"
_HELPERS = "anthropic.lib.streaming"
_CLOSE_TARGETS = (
    ("anthropic", "Stream", "close"),
    ("anthropic", "AsyncStream", "close"),
    (_HELPERS, "MessageStream", "close"),
    (_HELPERS, "AsyncMessageStream", "close"),
)
_PATCHED = (
    (_RESOURCES, "Messages", "create"), (_RESOURCES, "AsyncMessages", "create"),
    (_RESOURCES, "Messages", "stream"), (_RESOURCES, "AsyncMessages", "stream"),
    (_HELPERS, "MessageStreamManager", "__enter__"),
    (_HELPERS, "AsyncMessageStreamManager", "__aenter__"),
    *_CLOSE_TARGETS,
)  # fmt: skip
_CALL = "_gaslit_call"  # set on a stream() manager until it is entered
# A stream reports its errors as an event after HTTP 200; its error type gives the status.
_TYPE_STATUS = {
    "invalid_request_error": 400, "authentication_error": 401, "billing_error": 402,
    "permission_error": 403, "not_found_error": 404, "request_too_large": 413,
    "rate_limit_error": 429, "api_error": 500, "timeout_error": 504, "overloaded_error": 529,
}  # fmt: skip
_version: str | None = None


# --- Installing ---------------------------------------------------------------------------


def install(instrument: object = None) -> None:
    """Patch the ``anthropic`` library if it is installed and wanted. Idempotent, never raises."""
    global _version
    try:
        if not _wrap.wanted(instrument, "anthropic"):
            return
        if importlib.util.find_spec("anthropic") is None:
            return
        anthropic = importlib.import_module("anthropic")
        _version = _wrap.version(anthropic)
        hooks = _wrap.Hooks(_begin, _succeed, _fail)
        resources = importlib.import_module(_RESOURCES)
        classes = (resources.Messages, resources.AsyncMessages)
        for cls in classes:
            _wrap.patch(cls, "create", "messages", hooks, is_async=cls is classes[1])
        _wrap.patch_close(anthropic.Stream, is_async=False)
        _wrap.patch_close(anthropic.AsyncStream, is_async=True)
        try:
            helpers = importlib.import_module(_HELPERS)
            _patch_enter(helpers.MessageStreamManager, "__enter__", is_async=False)
            _patch_enter(helpers.AsyncMessageStreamManager, "__aenter__", is_async=True)
            _wrap.patch_close(helpers.MessageStream, is_async=False)
            _wrap.patch_close(helpers.AsyncMessageStream, is_async=True)
            for cls in classes:
                _patch_stream(cls)
        except Exception:
            _log.warn("anthropic_helper", "Could not instrument anthropic messages.stream().")
    except Exception:
        _log.warn("anthropic_patch", "Could not instrument the anthropic library.")


def uninstall() -> None:
    """Restore the original methods. For tests."""
    _wrap.unpatch(_PATCHED)


def _patch_stream(cls: Any) -> None:
    """``stream()`` only builds a manager (sync and async alike): note the call on it."""
    original = cls.stream
    if hasattr(original, _wrap.MARK):
        return

    @functools.wraps(original)
    def stream(self: Any, *args: Any, **kwargs: Any) -> Any:
        manager = original(self, *args, **kwargs)
        call = _begin(self, "messages", kwargs, helper=True)
        if call is not None:
            with contextlib.suppress(Exception):  # fail open, P4
                manager.__dict__[_CALL] = call
        return manager

    setattr(stream, _wrap.MARK, original)
    cls.stream = stream


def _patch_enter(cls: Any, name: str, *, is_async: bool) -> None:
    """Entering a ``stream()`` manager sends the request: the measured call starts here."""
    original = getattr(cls, name)
    if hasattr(original, _wrap.MARK):
        return
    if is_async:

        @functools.wraps(original)
        async def aenter(self: Any) -> Any:
            call = _take_call(self)
            if call is None:
                return await original(self)
            try:
                stream = await original(self)
            except BaseException as exc:
                _fail(call, exc)
                raise
            _entered(call, stream)
            return stream

        wrapper: Any = aenter
    else:

        @functools.wraps(original)
        def enter(self: Any) -> Any:
            call = _take_call(self)
            if call is None:
                return original(self)
            try:
                stream = original(self)
            except BaseException as exc:
                _fail(call, exc)
                raise
            _entered(call, stream)
            return stream

        wrapper = enter
    setattr(wrapper, _wrap.MARK, original)
    setattr(cls, name, wrapper)


def _take_call(manager: Any) -> Call | None:
    try:
        call: Call | None = manager.__dict__.pop(_CALL, None)
        if call is not None and _runtime.state is not None:
            call.ts_ms, call.start_ns = _now_ms(), time.perf_counter_ns()
            return call
    except Exception:  # noqa: S110 (fail open, P4)
        pass
    return None


def _entered(call: Call, stream: Any) -> None:
    """Measure the raw stream that the helper's ``MessageStream`` reads from."""
    try:
        raw = stream.__dict__.get("_raw_stream")
    except Exception:
        raw = None
    if raw is None:
        _log.warn("anthropic_helper", "The anthropic stream helper changed; timings are partial.")
    _succeed(call, raw)


# --- Recording ----------------------------------------------------------------------------


def _begin(
    resource: Any, operation: str, kwargs: dict[str, Any], *, helper: bool = False
) -> Call | None:
    """Start measuring, or return None when the SDK is off or anything goes wrong."""
    if _runtime.state is None:
        return None
    try:
        model = kwargs.get("model")
        endpoint = detect(resource._client.base_url, model)
        if endpoint.route == "azure":  # Microsoft Foundry: Claude on an Azure host
            endpoint = endpoint._replace(provider="anthropic")
        return Call(
            provider=endpoint.provider,
            route=endpoint.route,
            operation=operation,
            model_requested=_wrap.requested_model(endpoint, model),
            streaming=helper or kwargs.get("stream") is True,
            endpoint_host=endpoint.host,
            endpoint_region=endpoint.region,
            has_tools=bool(kwargs.get("tools")),
            provider_sdk="anthropic",
            provider_sdk_version=_version,
        )
    except Exception:
        return None


def _succeed(call: Call, result: Any) -> Any:
    """Finish or hand over a call that returned. Always returns ``result`` itself."""
    try:
        if call.streaming and _measure_stream(call, result):
            return result
        call.finish()
        _read_message(call, result)
        _read_usage(call, getattr(result, "usage", None))
        _runtime.record(call)
    except Exception:  # noqa: S110 (fail open, P4)
        pass
    return result


def _fail(call: Call, exc: BaseException) -> None:
    try:
        # A TypeError for a bad argument never reached the provider: not a reliability signal.
        if isinstance(exc, Exception) and not _is_anthropic_error(exc):
            return
        call.finish()
        _mark_error(call, exc)
        _runtime.record(call)
    except Exception:  # noqa: S110 (fail open, P4)
        pass


def _is_anthropic_error(exc: Exception) -> bool:
    return isinstance(exc, importlib.import_module("anthropic").AnthropicError)


def _mark_error(call: Call, exc: BaseException) -> None:
    """Classify by exception type, status and short error type. Message text is never read."""
    if not isinstance(exc, Exception):  # CancelledError, KeyboardInterrupt, SystemExit
        call.status = call.error_class = CANCELLED
        return
    anthropic = importlib.import_module("anthropic")
    call.status = ERROR
    if isinstance(exc, anthropic.APIStatusError):
        code = _error_type(getattr(exc, "body", None))
        status = call.http_status = _http_status(getattr(exc, "status_code", None))
        call.provider_error_code = code
        if status is None or status < 400:  # an error event inside a 200 stream
            status = _TYPE_STATUS.get(code or "")
        call.error_class = classify_http(status, code)
    elif isinstance(exc, anthropic.APITimeoutError):
        call.error_class = "timeout"
    elif isinstance(exc, anthropic.APIConnectionError):
        call.error_class = "connection_error"
    else:
        call.error_class = "unknown"


def _error_type(body: object) -> str | None:
    """``error.type`` from ``{"type": "error", "error": {"type": ..., "message": ...}}``."""
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("type") if isinstance(error, dict) else None
    return code if isinstance(code, str) and valid_error_code(code) else None


def _read_message(call: Call, message: Any) -> None:
    model = getattr(message, "model", None)
    if isinstance(model, str) and valid_model(model):
        call.model_returned = model


def _read_usage(call: Call, usage: Any) -> bool:
    """Take the token counts. True if the output count was found.

    Anthropic's ``input_tokens`` leaves out tokens read from or written to the prompt cache.
    They are added back, so ``input_tokens`` is the whole prompt, as for other providers.
    """
    if usage is None:
        return False
    if (n := _count(getattr(usage, "input_tokens", None))) is not None:
        read = _count(getattr(usage, "cache_read_input_tokens", None))
        written = _count(getattr(usage, "cache_creation_input_tokens", None)) or 0
        call.input_tokens = _count(n + (read or 0) + written)
        if read is not None:
            call.cached_input_tokens = read
    if (n := _count(getattr(usage, "output_tokens", None))) is None:
        return False
    call.output_tokens = n
    return True


# --- Streams ------------------------------------------------------------------------------


def _measure_stream(call: Call, stream: Any) -> bool:
    anthropic = importlib.import_module("anthropic")
    if isinstance(stream, anthropic.Stream):
        is_async = False
    elif isinstance(stream, anthropic.AsyncStream):
        is_async = True
    else:  # e.g. a raw-response wrapper: nothing to iterate here
        return False
    return _wrap.measure(stream, call, _event_item, _mark_error, is_async=is_async)


def _event_item(m: _wrap.Measure, event: Any) -> None:
    kind = event.type
    # Content arrives as deltas (text, tool input, thinking). message_start comes first, before
    # any output, so it is not the first token (brief §6.3).
    if kind == "content_block_delta":
        m.output()
    elif kind == "message_start":
        message = event.message
        _read_message(m.call, message)
        _read_usage(m.call, message.usage)  # the input counts; its output count is a placeholder
        m.call.output_tokens = None
    elif kind == "message_delta":
        if _read_usage(m.call, event.usage):  # the final, cumulative counts
            m.reported = True
    elif kind == "message_stop":
        m.finish(None)  # the reply is complete even if the user stops reading here
