"""What the library integrations share: patching methods, and measuring streams by swapping
their inner iterator (brief §6.3, ADR 0010, ADR 0011).

A wrapper never changes arguments, results or exceptions. Every entry point catches its own
failures, so nothing here can raise into user code (P4).
"""

from __future__ import annotations

import functools
import importlib
import inspect
import weakref
from collections.abc import Callable
from typing import Any, NamedTuple

from . import _log, _runtime
from ._hosts import Endpoint
from ._model import CANCELLED, Call, valid_model, valid_tag

MARK = "_gaslit_original"  # set on each wrapper; holds the method it replaced
AZURE_MODEL = "azure-deployment"  # Azure's "model" is the customer's own deployment name
BEDROCK_ARN = "bedrock-arn"  # a Bedrock ARN that names no public model
_BEDROCK_PUBLIC_ARNS = ("foundation-model/", "inference-profile/")


class Hooks(NamedTuple):
    """One library's parts: start measuring a call, then finish it with a result or error."""

    begin: Callable[[Any, str, dict[str, Any]], Call | None]
    succeed: Callable[[Call, Any], Any]
    fail: Callable[[Call, BaseException], None]


# --- Installing ---------------------------------------------------------------------------


def wanted(instrument: object, library: str) -> bool:
    """Whether ``init(instrument=...)`` asks for this library. None means every library."""
    try:
        return instrument is None or library in instrument  # type: ignore[operator]
    except TypeError:
        return True


def version(module: Any) -> str | None:
    found = getattr(module, "__version__", None)
    return found if isinstance(found, str) and valid_tag(found) else None


def patch(cls: Any, name: str, operation: str, hooks: Hooks, *, is_async: bool) -> None:
    original = getattr(cls, name)
    if hasattr(original, MARK):  # already wrapped
        return
    if not is_async:
        wrapper = _sync_call(original, operation, hooks)
    elif inspect.iscoroutinefunction(original):
        wrapper = _async_call(original, operation, hooks)
    else:  # e.g. @required_args wraps an async def in a plain function
        wrapper = _async_returning_call(original, operation, hooks)
    setattr(wrapper, MARK, original)
    setattr(cls, name, wrapper)


def patch_close(cls: Any, *, is_async: bool) -> None:
    """Closing a stream early (or leaving its ``with`` block) cancels the measured call."""
    original = cls.close
    if hasattr(original, MARK):
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
    setattr(wrapper, MARK, original)
    cls.close = wrapper


def unpatch(targets: tuple[tuple[str, str, str], ...]) -> None:
    """Restore the original methods named by (module, class, method). For tests."""
    for module, name, method in targets:
        try:
            cls = getattr(importlib.import_module(module), name)
            original = getattr(getattr(cls, method), MARK, None)
            if original is not None:
                setattr(cls, method, original)
        except Exception:  # noqa: S112 (that target was never patched)
            continue


def _cancel_open(stream: Any) -> None:
    try:
        measure = stream.__dict__.get("_iterator")
        if not isinstance(measure, Measure):  # a helper stream around the measured raw one
            raw = stream.__dict__.get("_raw_stream")
            measure = raw.__dict__.get("_iterator") if raw is not None else None
        if isinstance(measure, Measure):
            measure.finish(None, cancelled=True)
    except Exception:  # noqa: S110 (fail open, P4)
        pass


# --- Wrapping calls -----------------------------------------------------------------------


def _sync_call(original: Callable[..., Any], operation: str, h: Hooks) -> Callable[..., Any]:
    @functools.wraps(original)
    def call(self: Any, *args: Any, **kwargs: Any) -> Any:
        measured = h.begin(self, operation, kwargs)
        if measured is None:
            return original(self, *args, **kwargs)
        try:
            result = original(self, *args, **kwargs)
        except BaseException as exc:
            h.fail(measured, exc)
            raise
        return h.succeed(measured, result)

    return call


def _async_call(original: Callable[..., Any], operation: str, h: Hooks) -> Callable[..., Any]:
    @functools.wraps(original)
    async def call(self: Any, *args: Any, **kwargs: Any) -> Any:
        measured = h.begin(self, operation, kwargs)
        if measured is None:
            return await original(self, *args, **kwargs)
        try:
            result = await original(self, *args, **kwargs)
        except BaseException as exc:
            h.fail(measured, exc)
            raise
        return h.succeed(measured, result)

    return call


def _async_returning_call(
    original: Callable[..., Any], operation: str, h: Hooks
) -> Callable[..., Any]:
    """For a plain function that returns a coroutine: stay a plain function, as it was."""

    @functools.wraps(original)
    def call(self: Any, *args: Any, **kwargs: Any) -> Any:
        measured = h.begin(self, operation, kwargs)
        if measured is None:
            return original(self, *args, **kwargs)
        try:
            pending = original(self, *args, **kwargs)
        except BaseException as exc:
            h.fail(measured, exc)
            raise
        return _await(measured, pending)

    async def _await(measured: Call, pending: Any) -> Any:
        try:
            result = await pending
        except BaseException as exc:
            h.fail(measured, exc)
            raise
        return h.succeed(measured, result)

    return call


# --- Model names --------------------------------------------------------------------------


def requested_model(endpoint: Endpoint, model: object) -> str:
    """``model_requested`` for a call. Names that identify the customer are replaced."""
    if endpoint.route == "azure":
        return AZURE_MODEL
    if not isinstance(model, str) or not valid_model(model):
        return "invalid"
    if endpoint.route == "bedrock" and model.startswith("arn:"):
        # arn:aws:bedrock:<region>:<account>:<type>/<id>. Keep the id only where it is a public
        # model name, so the AWS account id and private resource ids are never sent.
        resource = model.split(":", 5)[-1]
        if resource.startswith(_BEDROCK_PUBLIC_ARNS):
            return resource.split("/", 1)[1] or BEDROCK_ARN
        return BEDROCK_ARN
    return model


# --- Streams ------------------------------------------------------------------------------


def measure(
    stream: Any,
    call: Call,
    on_item: Callable[[Measure, Any], None],
    on_error: Callable[[Call, BaseException], None],
    *,
    is_async: bool,
) -> bool:
    """Swap the stream's inner iterator for a measuring one. The stream object itself is
    untouched, so ``isinstance``, ``with`` and ``close()`` behave as before."""
    inner = getattr(stream, "_iterator", None)
    if inner is None:
        library = call.provider_sdk or "provider"
        _log.warn(
            f"{library}_stream", f"The {library} stream layout changed; stream timings are partial."
        )
        return False
    measuring = (AsyncMeasure if is_async else SyncMeasure)(call, inner, on_item, on_error)
    stream._iterator = measuring
    # If the stream is dropped unfinished, record it as cancelled. The callback holds only the
    # open-call box, never the stream, so it can't keep the stream alive.
    weakref.finalize(stream, _abandon, measuring.open).atexit = False
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


class Measure:
    """Stands in for a stream's inner iterator and times it, one item at a time."""

    __slots__ = ("_inner", "_on_error", "_on_item", "_outputs", "call", "open", "reported")

    def __init__(
        self,
        call: Call,
        inner: Any,
        on_item: Callable[[Measure, Any], None],
        on_error: Callable[[Call, BaseException], None],
    ) -> None:
        self.call = call
        self._inner = inner
        self._on_item = on_item
        self._on_error = on_error
        self._outputs = 0
        self.reported = False  # the stream gave us an output token count
        self.open = [call]  # emptied by whichever of finish / cancel / abandon comes first

    def output(self) -> None:
        if self.call.ttft_ms is None:
            self.call.first_token()
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
                self._on_error(call, exc)
            elif not self.reported and self._outputs:
                # No usage in the stream: count output chunks as a rough token estimate.
                call.output_tokens = self._outputs
                call.output_tokens_estimated = True
            _runtime.record(call)
        except Exception:  # noqa: S110 (fail open, P4)
            pass


class SyncMeasure(Measure):
    __slots__ = ()

    def __iter__(self) -> SyncMeasure:
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


class AsyncMeasure(Measure):
    __slots__ = ()

    def __aiter__(self) -> AsyncMeasure:
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
