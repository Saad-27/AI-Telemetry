"""yourpkg: passive reliability metrics for AI model calls.

Importing this package must have no side effects: no patching, no threads,
no network and no environment reads. All of that happens in ``init()`` (brief §11.7).
Internals are imported lazily, so even the stdlib modules they need load only on first use.
"""

TYPE_CHECKING = False
if TYPE_CHECKING:
    from ._track import Tracker

__version__ = "0.0.0"

__all__ = ["__version__", "init", "track"]


def init(
    api_key: str | None = None,
    *,
    service: str | None = None,
    environment: str | None = None,
    endpoint: str | None = None,
    enabled: bool = True,
    debug: bool = False,
    flush_interval: float = 5.0,
    max_batch: int = 200,
    max_queue: int = 10_000,
    sample_rate: float = 0.05,
    max_samples_per_minute: int = 10,
    origin_region: str | None = None,
) -> None:
    """Start measuring. Reads ``YOURPKG_KEY`` and other ``YOURPKG_*`` variables.

    Idempotent and never raises. Without a key it warns once and does nothing,
    unless ``debug=True``, which prints payloads locally instead of sending them.
    """
    try:
        from . import _runtime

        _runtime.init(
            enabled,
            api_key=api_key,
            service=service,
            environment=environment,
            endpoint=endpoint,
            debug=debug,
            flush_interval=flush_interval,
            max_batch=max_batch,
            max_queue=max_queue,
            sample_rate=sample_rate,
            max_samples_per_minute=max_samples_per_minute,
            origin_region=origin_region,
        )
    except Exception:  # noqa: S110 (fail open, P4)
        pass


def track(
    provider: str,
    model: str,
    *,
    label: str | None = None,
    streaming: bool = False,
    route: str | None = None,
) -> "Tracker":
    """Measure a call the SDK can't instrument automatically.

    ::

        with yourpkg.track(provider="custom", model="my-finetune", streaming=True) as t:
            for chunk in call_my_model(...):
                t.first_token()
            t.set_usage(input_tokens=1234, output_tokens=210)

    Timing starts when ``track()`` is called. An exception inside the block is recorded
    and re-raised unchanged. Use ``t.set_error(http_status, code)`` for failures that
    don't raise.
    """
    from ._track import track as _track

    return _track(provider, model, label=label, streaming=streaming, route=route)
