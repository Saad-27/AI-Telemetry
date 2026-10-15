"""OpenAI integration: measures ``chat.completions.create`` and ``responses.create``
(brief §6.2, §6.3, ADR 0010).

The ``create`` methods of the resource classes are wrapped, so clients made before ``init()``
are covered, and so are the Azure client, OpenAI-compatible endpoints and the library's own
helpers, which call ``create``. Arguments, results and exceptions pass through untouched.

Read from a call: the model name, whether ``stream`` and ``tools`` were given (a bool, not the
tools), the client's hostname, the usage counts, the returned model, the exception type, the
HTTP status and the short error code. Never read: messages, outputs, tool data, keys, headers,
URL paths or queries, or error message text (P1).

Every entry point catches its own failures, so nothing here can raise into user code (P4).
"""

from __future__ import annotations

import functools
import importlib
import importlib.util
import inspect
import weakref
from collections.abc import Callable
from typing import Any

from . import _log, _runtime
from ._hosts import detect
from ._model import (
    CANCELLED,
    ERROR,
    Call,
    classify_http,
    valid_error_code,
    valid_model,
    valid_tag,
)
from ._track import _count, _http_status

# (module, class, operation). Resolved by name at install time, because paths move between
# library versions; a target that isn't found is skipped, not fatal.
_TARGETS = (
    ("openai.resources.chat.completions", "Completions", "chat.completions"),
    ("openai.resources.chat.completions", "AsyncCompletions", "chat.completions"),
    ("openai.resources.responses", "Responses", "responses"),
    ("openai.resources.responses", "AsyncResponses", "responses"),
)
_TERMINAL_EVENTS = ("response.completed", "response.incomplete", "response.failed")
_MARK = "_gaslit_original"
_AZURE_MODEL = "azure-deployment"  # Azure's "model" is the customer's own deployment name
_version: str | None = None


# --- Installing ---------------------------------------------------------------------------


def install(instrument: object = None) -> None:
    """Patch the ``openai`` library if it is installed and wanted. Idempotent, never raises."""
    global _version
    try:
        try:
            wanted = instrument is None or "openai" in instrument  # type: ignore[operator]
        except TypeError:
            wanted = True
        if not wanted or importlib.util.find_spec("openai") is None:
            return
        openai = importlib.import_module("openai")
        version = getattr(openai, "__version__", None)
        _version = version if isinstance(version, str) and valid_tag(version) else None
        for module, name, operation in _TARGETS:
            try:
                cls = getattr(importlib.import_module(module), name)
                _patch(cls, "create", operation, is_async=name.startswith("Async"))
            except ImportError:
                pass  # this openai version doesn't have that API (Responses is newer than 1.66)
            except Exception:
                _log.warn("openai_path", f"Could not instrument openai {name}; skipping it.")
        _patch_close(openai.Stream, is_async=False)
        _patch_close(openai.AsyncStream, is_async=True)
    except Exception:
        _log.warn("openai_patch", "Could not instrument the openai library.")


def uninstall() -> None:
    """Restore the original methods. For tests."""
    targets = [(m, n, "create") for m, n, _ in _TARGETS]
    targets += [("openai", "Stream", "close"), ("openai", "AsyncStream", "close")]
    for module, name, method in targets:
        try:
            cls = getattr(importlib.import_module(module), name)
            original = getattr(getattr(cls, method), _MARK, None)
            if original is not None:
                setattr(cls, method, original)
        except Exception:  # noqa: S112 (that target was never patched)
            continue


def _patch(cls: Any, name: str, operation: str, *, is_async: bool) -> None:
    original = getattr(cls, name)
    if hasattr(original, _MARK):  # already wrapped
        return
    if not is_async:
        wrapper = _sync_create(original, operation)
    elif inspect.iscoroutinefunction(original):
        wrapper = _async_create(original, operation)
    else:  # e.g. openai's @required_args wraps an async def in a plain function
        wrapper = _async_returning_create(original, operation)
    setattr(wrapper, _MARK, original)
    setattr(cls, name, wrapper)


def _patch_close(cls: Any, *, is_async: bool) -> None:
    """Closing a stream early (or leaving its ``with`` block) cancels the measured call."""
    original = cls.close
    if hasattr(original, _MARK):
        return
    if is_async:

        @functools.wraps(original)
        async def aclose(self: Any) -> Any:
            _cancel_open(self)
            return await original(self)

        wrapper: Any = aclose
    else:

        @functools.wraps(original)
        def close(self: Any) -> Any:
            _cancel_open(self)
            return original(self)

        wrapper = close
    setattr(wrapper, _MARK, original)
    cls.close = wrapper


def _cancel_open(stream: Any) -> None:
    try:
        measure = stream.__dict__.get("_iterator")
        if isinstance(measure, _Measure):
            measure.finish(None, cancelled=True)
    except Exception:  # noqa: S110 (fail open, P4)
        pass


# --- Wrapping create ----------------------------------------------------------------------


def _sync_create(original: Callable[..., Any], operation: str) -> Callable[..., Any]:
    @functools.wraps(original)
    def create(self: Any, *args: Any, **kwargs: Any) -> Any:
        call = _begin(self, operation, kwargs)
        if call is None:
            return original(self, *args, **kwargs)
        try:
            result = original(self, *args, **kwargs)
        except BaseException as exc:
            _fail(call, exc)
            raise
        return _succeed(call, result)

    return create


def _async_create(original: Callable[..., Any], operation: str) -> Callable[..., Any]:
    @functools.wraps(original)
    async def create(self: Any, *args: Any, **kwargs: Any) -> Any:
        call = _begin(self, operation, kwargs)
        if call is None:
            return await original(self, *args, **kwargs)
        try:
            result = await original(self, *args, **kwargs)
        except BaseException as exc:
            _fail(call, exc)
            raise
        return _succeed(call, result)

    return create


def _async_returning_create(original: Callable[..., Any], operation: str) -> Callable[..., Any]:
    """For a plain function that returns a coroutine: stay a plain function, as it was."""

    @functools.wraps(original)
    def create(self: Any, *args: Any, **kwargs: Any) -> Any:
        call = _begin(self, operation, kwargs)
        if call is None:
            return original(self, *args, **kwargs)
        try:
            pending = original(self, *args, **kwargs)
        except BaseException as exc:
            _fail(call, exc)
            raise
        return _await(call, pending)

    async def _await(call: Call, pending: Any) -> Any:
        try:
            result = await pending
        except BaseException as exc:
            _fail(call, exc)
            raise
        return _succeed(call, result)

    return create


# --- Recording ----------------------------------------------------------------------------


def _begin(resource: Any, operation: str, kwargs: dict[str, Any]) -> Call | None:
    """Start measuring, or return None when the SDK is off or anything goes wrong."""
    if _runtime.state is None:
        return None
    try:
        model = kwargs.get("model")
        endpoint = detect(resource._client.base_url, model)
        if endpoint.route == "azure":
            requested = _AZURE_MODEL
        else:
            requested = model if isinstance(model, str) and valid_model(model) else "invalid"
        return Call(
            provider=endpoint.provider,
            route=endpoint.route,
            operation=operation,
            model_requested=requested,
            streaming=kwargs.get("stream") is True,
            endpoint_host=endpoint.host,
            endpoint_region=endpoint.region,
            has_tools=bool(kwargs.get("tools") or kwargs.get("functions")),
            provider_sdk="openai",
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
        _read_result(call, result)
        _runtime.record(call)
    except Exception:  # noqa: S110 (fail open, P4)
        pass
    return result


def _fail(call: Call, exc: BaseException) -> None:
    try:
        # A TypeError for a bad argument never reached the provider: not a reliability signal.
        if isinstance(exc, Exception) and not _is_openai_error(exc):
            return
        call.finish()
        _mark_error(call, exc)
        _runtime.record(call)
    except Exception:  # noqa: S110 (fail open, P4)
        pass


def _is_openai_error(exc: Exception) -> bool:
    return isinstance(exc, importlib.import_module("openai").OpenAIError)


def _mark_error(call: Call, exc: BaseException) -> None:
    """Classify by exception type, status and short code. Message text is never read."""
    if not isinstance(exc, Exception):  # CancelledError, KeyboardInterrupt, SystemExit
        call.status = call.error_class = CANCELLED
        return
    openai = importlib.import_module("openai")
    call.status = ERROR
    if isinstance(exc, openai.APIStatusError):
        code = getattr(exc, "code", None)
        call.http_status = _http_status(getattr(exc, "status_code", None))
        call.provider_error_code = (
            code if isinstance(code, str) and valid_error_code(code) else None
        )
        call.error_class = classify_http(call.http_status, call.provider_error_code)
    elif isinstance(exc, openai.APITimeoutError):
        call.error_class = "timeout"
    elif isinstance(exc, openai.APIConnectionError):
        call.error_class = "connection_error"
    else:
        call.error_class = "unknown"


def _read_result(call: Call, result: Any) -> bool:
    """Take the returned model and usage from a response. True if usage was found."""
    model = getattr(result, "model", None)
    if isinstance(model, str) and valid_model(model):
        call.model_returned = model
    if getattr(result, "status", None) == "failed":  # a Responses call can fail with HTTP 200
        error = getattr(result, "error", None)
        code = getattr(error, "code", None)
        call.status = ERROR
        call.provider_error_code = (
            code if isinstance(code, str) and valid_error_code(code) else None
        )
        call.error_class = classify_http(None, call.provider_error_code)
    usage = getattr(result, "usage", None)
    return usage is not None and _read_usage(call, usage)


# Chat names the counts prompt/completion_tokens, Responses input/output_tokens. Looked up by
# operation, because asking a pydantic model for a field it lacks is slow.
_USAGE_FIELDS = {
    "chat.completions": ("prompt_tokens", "completion_tokens", "prompt_tokens_details"),
    "responses": ("input_tokens", "output_tokens", "input_tokens_details"),
}


def _read_usage(call: Call, usage: Any) -> bool:
    inp, out, details = _USAGE_FIELDS[call.operation]
    if (n := _count(getattr(usage, inp, None))) is not None:
        call.input_tokens = n
    if (n := _count(getattr(getattr(usage, details, None), "cached_tokens", None))) is not None:
        call.cached_input_tokens = n
    if (n := _count(getattr(usage, out, None))) is None:
        return False
    call.output_tokens = n
    return True


# --- Streams ------------------------------------------------------------------------------


def _measure_stream(call: Call, stream: Any) -> bool:
    """Swap the stream's inner iterator for a measuring one. The stream object itself is
    untouched, so ``isinstance``, ``with`` and ``close()`` behave as before."""
    openai = importlib.import_module("openai")
    if isinstance(stream, openai.Stream):
        cls: type[_Measure] = _SyncMeasure
    elif isinstance(stream, openai.AsyncStream):
        cls = _AsyncMeasure
    else:  # e.g. a raw-response wrapper: nothing to iterate here
        return False
    inner = getattr(stream, "_iterator", None)
    if inner is None:
        _log.warn("openai_stream", "The openai stream layout changed; stream timings are partial.")
        return False
    measure = cls(call, inner, _chat_item if call.operation == "chat.completions" else _event_item)
    stream._iterator = measure
    # If the stream is dropped unfinished, record it as cancelled. The callback holds only the
    # open-call box, never the stream, so it can't keep the stream alive.
    weakref.finalize(stream, _abandon, measure.open).atexit = False
    return True


def _abandon(open_calls: list[Call]) -> None:
    # Runs in a garbage-collection finalizer, possibly while this thread holds an SDK lock.
    try:
        call = open_calls.pop()
    except IndexError:
        return
    call.finish()
    call.status = call.error_class = CANCELLED
    _runtime.abandon(call)


class _Measure:
    """Stands in for a stream's inner iterator and times it, one item at a time."""

    __slots__ = ("_call", "_inner", "_on_item", "_outputs", "open", "reported")

    def __init__(self, call: Call, inner: Any, on_item: Callable[[_Measure, Any], None]) -> None:
        self._call = call
        self._inner = inner
        self._on_item = on_item
        self._outputs = 0
        self.reported = False  # the stream gave us a usage count
        self.open = [call]  # emptied by whichever of finish / cancel / abandon comes first

    def output(self) -> None:
        if self._call.ttft_ms is None:
            self._call.first_token()
        self._outputs += 1

    def item(self, item: Any) -> None:
        try:  # noqa: SIM105 (per-chunk path: cheaper than contextlib.suppress)
            self._on_item(self, item)
        except Exception:  # noqa: S110 (fail open, P4)
            pass

    def finish(self, exc: BaseException | None, *, cancelled: bool = False) -> None:
        try:
            call = self.open.pop()
        except IndexError:
            return
        try:
            call.finish()
            if cancelled:
                call.status = call.error_class = CANCELLED
            elif exc is not None:
                _mark_error(call, exc)
            elif not self.reported and self._outputs:
                # No usage in the stream: count output chunks as a rough token estimate.
                call.output_tokens = self._outputs
                call.output_tokens_estimated = True
            _runtime.record(call)
        except Exception:  # noqa: S110 (fail open, P4)
            pass


class _SyncMeasure(_Measure):
    __slots__ = ()

    def __iter__(self) -> _SyncMeasure:
        return self

    def __next__(self) -> Any:
        try:
            item = next(self._inner)
        except StopIteration:
            self.finish(None)
            raise
        except BaseException as exc:
            self.finish(exc)
            raise
        self.item(item)
        return item


class _AsyncMeasure(_Measure):
    __slots__ = ()

    def __aiter__(self) -> _AsyncMeasure:
        return self

    async def __anext__(self) -> Any:
        try:
            item = await self._inner.__anext__()
        except StopAsyncIteration:
            self.finish(None)
            raise
        except BaseException as exc:
            self.finish(exc)
            raise
        self.item(item)
        return item


def _chat_item(m: _Measure, chunk: Any) -> None:
    call = m._call
    if call.model_returned is None:
        model = getattr(chunk, "model", None)
        if isinstance(model, str) and valid_model(model):
            call.model_returned = model
    # The first chunk with output is the first token, not the first event (brief §6.3).
    for choice in getattr(chunk, "choices", None) or ():
        delta = choice.delta
        if (
            delta.content
            or delta.tool_calls
            or getattr(delta, "function_call", None)
            or getattr(delta, "refusal", None)
            or _reasoning(delta)
        ):
            m.output()
            break
    usage = getattr(chunk, "usage", None)  # only present if the user set include_usage (P3)
    if usage is not None and _read_usage(call, usage):
        m.reported = True


def _reasoning(delta: Any) -> bool:
    # Compatible providers send thinking as an extra field (e.g. DeepSeek's reasoning_content).
    # Last in the chain: reading pydantic's extras is slow, and most chunks carry content.
    extra = getattr(delta, "model_extra", None)
    return bool(extra and (extra.get("reasoning_content") or extra.get("reasoning")))


def _event_item(m: _Measure, event: Any) -> None:
    kind = getattr(event, "type", "")
    if kind.endswith(".delta"):  # text, tool-call arguments, reasoning, refusal, audio
        m.output()
    elif kind in _TERMINAL_EVENTS and _read_result(m._call, event.response):
        m.reported = True
