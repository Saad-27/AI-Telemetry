# 0010: OpenAI integration (wrapping, streams, installing)

- **Status:** accepted (the Azure placeholder in point 5 is waiting for Saad)
- **Date:** 2026-10-15

## Context
Brief §6.3 asks for two choices to be recorded: method-level wrapping versus HTTP hooks, and how
to wrap streams. The code is `sdk/python/src/gaslit/_openai.py`. It was checked on 2026-10-15
against the `openai` source (latest 3.31.0, which now uses `httpx2`) and run against 1.30.0,
1.66.0, 2.0.0 and 3.31.0 (brief §17).

## Decision
1. **Wrap `create` on the four resource classes** (`Completions`, `AsyncCompletions`,
   `Responses`, `AsyncResponses`), not the HTTP transport. Classes are patched, so clients made
   before `init()` are covered, and so are `AzureOpenAI`, compatible endpoints and the library's
   own helpers, which call `create`. A transport hook may come later, only for attempt counts
   and numeric rate-limit headers.
2. **Wrapper shape follows the original.** `AsyncCompletions.create` is a plain function that
   returns a coroutine (because of `@required_args`), while `AsyncResponses.create` is
   `async def`. Each keeps its shape, so `inspect.iscoroutinefunction` and the signature don't
   change.
3. **Streams: replace the stream's private `_iterator`.** `Stream.__next__`/`__iter__` (and the
   async pair) read only that attribute, so the stream object keeps its type, `with` support
   and `close()`. `Stream.close` and `AsyncStream.close` are patched once at class level, so
   leaving a `with` block early marks the call `cancelled`. If `_iterator` is missing in a future
   version, the call is still counted with partial timings and one warning.
4. **A stream dropped unfinished is `cancelled`, via `weakref.finalize`.** A finalizer can run on
   any thread while it holds one of the SDK's locks, so it only appends the call to a lock-free
   deque (`_runtime.abandon`, bounded at 10,000); the next normal call, `flush()` or `shutdown()`
   records it, in a loop rather than recursively. A forked child starts with the deque empty. The
   callback holds the open call, never the stream, so it can't keep the stream alive (tested).
5. **Azure: `model_requested` is the fixed text `azure-deployment`.** On Azure the `model`
   argument is the customer's own deployment name, which can contain a company or project
   name. The returned model (for example `gpt-4o-2024-08-06`) is still reported when there is a
   response. *Saad to confirm, or choose to send the deployment name.*
6. **What counts as a call.** Exceptions from the `openai` library are classified by type, status
   and short code. Cancellation (`CancelledError`, `KeyboardInterrupt`) is `cancelled`. Any other
   exception (for example a `TypeError` for a bad argument) never reached the provider and is
   not recorded. A Responses call that returns HTTP 200 with `status: failed` is an error.
7. **Chat streams without `include_usage`** have no token counts, and the SDK never adds the
   option (P3). The output tokens are then the number of chunks that carried output, flagged as
   an estimate. If the stream reports usage, that wins.
8. **Installing.** `init()` imports `openai` if it is installed and patches it (so an app that
   imports `openai` later is still covered). `instrument=[...]` selects libraries and `[]`
   turns them all off. `GASLIT_ENABLED=false` still means no patching at all.
9. **Supported versions:** `openai` 1.30 and newer (chat completions; Responses from 1.66),
   Python 3.10+. CI runs the OpenAI tests on 1.30.0, 1.66.0 and 2.0.0, and on the locked latest.

## Trade-offs
- `_iterator` is private. A library change would reduce stream timings to partial until the
  wrapper is updated; the compatibility job in CI is there to notice that.
- Calls through `with_raw_response` and `with_streaming_response` are counted for status and
  duration only: the result is a wrapper we can't read without consuming it. `parse()` and
  `stream()` helpers on chat/Responses that don't go through `create` are not measured yet.
- Importing `openai` inside `init()` costs its import time for apps that have it installed but
  don't use it. A post-import hook would avoid that but is more fragile.
- Before 1.30 (for example 1.0.0) there is no `tools`, `stream_options` or `error.code`, so those
  calls would be measured with less detail. Not supported.
