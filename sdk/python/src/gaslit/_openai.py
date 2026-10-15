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

import importlib
import importlib.util
from typing import Any

from . import _log, _runtime, _wrap
from ._hosts import detect
from ._model import CANCELLED, ERROR, Call, classify_http, valid_error_code, valid_model
from ._track import _count, _http_status

# (module, class, operation). Resolved by name at install time, because paths move between
# library versions; a target that isn't found is skipped, not fatal.
_TARGETS = (
    ("openai.resources.chat.completions", "Completions", "chat.completions"),
    ("openai.resources.chat.completions", "AsyncCompletions", "chat.completions"),
    ("openai.resources.responses", "Responses", "responses"),
    ("openai.resources.responses", "AsyncResponses", "responses"),
)
_CLOSE_TARGETS = (("openai", "Stream", "close"), ("openai", "AsyncStream", "close"))
_TERMINAL_EVENTS = ("response.completed", "response.incomplete", "response.failed")
_version: str | None = None


# --- Installing ---------------------------------------------------------------------------


def install(instrument: object = None) -> None:
    """Patch the ``openai`` library if it is installed and wanted. Idempotent, never raises."""
    global _version
    try:
        if not _wrap.wanted(instrument, "openai") or importlib.util.find_spec("openai") is None:
            return
        openai = importlib.import_module("openai")
        _version = _wrap.version(openai)
        hooks = _wrap.Hooks(_begin, _succeed, _fail)
        for module, name, operation in _TARGETS:
            try:
                cls = getattr(importlib.import_module(module), name)
                _wrap.patch(cls, "create", operation, hooks, is_async=name.startswith("Async"))
            except ImportError:
                pass  # this openai version doesn't have that API (Responses is newer than 1.66)
            except Exception:
                _log.warn("openai_path", f"Could not instrument openai {name}; skipping it.")
        _wrap.patch_close(openai.Stream, is_async=False)
        _wrap.patch_close(openai.AsyncStream, is_async=True)
    except Exception:
        _log.warn("openai_patch", "Could not instrument the openai library.")


def uninstall() -> None:
    """Restore the original methods. For tests."""
    _wrap.unpatch(tuple((m, n, "create") for m, n, _ in _TARGETS) + _CLOSE_TARGETS)


# --- Recording ----------------------------------------------------------------------------


def _begin(resource: Any, operation: str, kwargs: dict[str, Any]) -> Call | None:
    """Start measuring, or return None when the SDK is off or anything goes wrong."""
    if _runtime.state is None:
        return None
    try:
        model = kwargs.get("model")
        endpoint = detect(resource._client.base_url, model)
        return Call(
            provider=endpoint.provider,
            route=endpoint.route,
            operation=operation,
            model_requested=_wrap.requested_model(endpoint, model),
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
    openai = importlib.import_module("openai")
    if isinstance(stream, openai.Stream):
        is_async = False
    elif isinstance(stream, openai.AsyncStream):
        is_async = True
    else:  # e.g. a raw-response wrapper: nothing to iterate here
        return False
    on_item = _chat_item if call.operation == "chat.completions" else _event_item
    return _wrap.measure(stream, call, on_item, _mark_error, is_async=is_async)


def _chat_item(m: _wrap.Measure, chunk: Any) -> None:
    call = m.call
    if call.model_returned is None:
        model = getattr(chunk, "model", None)
        if isinstance(model, str) and valid_model(model):
            call.model_returned = model
    # The first chunk with output is the first token, not the first event (brief §6.3).
    for choice in getattr(chunk, "choices", None) or ():
        delta = choice.delta
        if delta is None:  # some compatible providers send null deltas, e.g. with usage
            continue
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


def _event_item(m: _wrap.Measure, event: Any) -> None:
    kind = getattr(event, "type", "")
    if kind.endswith(".delta"):  # text, tool-call arguments, reasoning, refusal, audio
        m.output()
    elif kind in _TERMINAL_EVENTS and _read_result(m.call, event.response):
        m.reported = True
