# 0011: Anthropic integration (create, the stream helper, cloud names)

- **Status:** accepted
- **Date:** 2026-10-15

## Context
Brief §6.2 and §6.3 ask for `messages.create` (sync and async, streaming or not) and the
`messages.stream()` helper, including the `AnthropicBedrock` and `AnthropicVertex` clients. The
code is `sdk/python/src/gaslit/_anthropic.py`. It was checked on 2026-10-15 against the
`anthropic` source (latest 1.15.0, which uses `httpx2`) and run against 0.28.0, 0.50.0,
0.124.0, 1.0.0 and 1.15.0 (brief §17).

## Decision
1. **Same approach as OpenAI (ADR 0010).** `create` is wrapped on `Messages` and
   `AsyncMessages`, and a stream is measured by swapping its `_iterator`. In 1.x `_iterator` is
   a `cached_property`, which is a plain attribute once set, so the same swap works. The parts
   both integrations use (method wrappers, stream measuring, close and drop handling) moved to
   `_wrap.py`; each integration keeps only its call details, errors and stream events.
2. **The `stream()` helper is wrapped in two places.** It does not call `create`: it returns a
   `MessageStreamManager` that sends the request on `__enter__`. So `stream()` notes the call on
   the manager, and the manager's `__enter__` (`__aenter__` for async) starts the clock, records
   a failed request, and measures the raw stream inside the `MessageStream` it returns. A
   manager that is never entered sends nothing and is not counted. The manager and stream
   classes are unchanged, so `isinstance`, `text_stream` and `get_final_message()` still work.
3. **`message_stop` finishes the call**, so a user who stops reading there (or uses
   `get_final_message()`) gets `ok`, not `cancelled`. Leaving the stream earlier, closing it or
   dropping it is `cancelled`, as for OpenAI. `MessageStream.close` is patched too, because in
   older versions it closes the response without going through the raw stream.
4. **First token is the first `content_block_delta`** (text, tool input or thinking), not
   `message_start`, which arrives before any output (brief §6.3).
5. **Errors inside a stream.** Anthropic can fail a stream after HTTP 200 with an `error` event
   (often `overloaded_error`). The client raises `APIStatusError` with status 200. The call keeps
   `http_status: 200` and the short type (`error.type` from the body, never the message), and the
   class comes from the type: `overloaded_error` is `overloaded`, `api_error` is `server_error`,
   and so on. `billing_error` is `quota_error` and `timeout_error` is `timeout` (brief §6.5).
6. **Customer names in cloud endpoints are not sent.** Bedrock model ARNs contain the AWS
   account id: for `foundation-model/...` and `inference-profile/...` ARNs only the model id after
   the `/` is kept, and any other ARN (for example an application inference profile) becomes
   `bedrock-arn`. Claude on Microsoft Foundry is reached through `*.services.ai.azure.com`, so it
   follows the Azure rule (ADR 0010 point 5): `provider: anthropic`, `route: azure`,
   `model_requested: azure-deployment`. Saad confirmed both rules on 2026-10-15.
7. **Input tokens include the prompt cache.** Anthropic's `input_tokens` leaves out tokens read
   from or written to the cache, while OpenAI's include them. The SDK adds them back, so
   `input_tokens` is the whole prompt for every provider and the input buckets compare like with
   like. `cached_input_tokens` is `cache_read_input_tokens`.
8. **Vertex multi-region hosts** `aiplatform.us.rep.googleapis.com` and
   `aiplatform.eu.rep.googleapis.com` are now on the host list (region `us` or `eu`): the
   `anthropic` client uses them for `region="us"` and `"eu"`. This closes a gap left in ADR 0009.
9. **Supported versions:** `anthropic` 0.28 and newer, Python 3.10+. CI runs the Anthropic
   tests on 0.28.0, 0.50.0 and 0.124.0, and on the locked latest.

## Trade-offs
- Not measured yet: `client.beta.messages` (including the tool runner), `messages.parse()`,
  `count_tokens` and batches. `with_raw_response` calls are counted for status and duration
  only, as for OpenAI.
- Newer clients reach Claude through other hosts that are not on the list yet, so they are
  `custom` with no host: Bedrock Mantle (`bedrock-mantle.<region>.api.aws`), `AnthropicAWS` and
  `AnthropicGoogleCloud` (`claude.googleapis.com`). Adding them needs a route decision.
- Overhead (benchmark): about 13 µs per call and 0.4 µs per streamed event, within the §6.8
  budget.
- The helper relies on two private names, `_iterator` and `MessageStream._raw_stream`. If either
  changes, the call is still counted with partial timings and one warning; the compatibility
  job in CI is there to notice that.
