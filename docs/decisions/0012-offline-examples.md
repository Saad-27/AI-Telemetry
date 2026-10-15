# 0012: Examples run offline through the fake provider as an HTTP proxy

- **Status:** accepted
- **Date:** 2026-10-15

## Context
Brief §9.11 and the M2 exit criterion ask for examples (OpenAI chat, an Anthropic summariser,
Azure/Bedrock/Vertex host detection "using mocks", manual `track()`) that produce correct
events. Gaslit reads only the client's hostname (§6.6). Pointing a client at the fake provider
on `127.0.0.1` makes every event `local`, which hides what the examples are meant to show.

## Options considered
1. **Fake provider on `127.0.0.1` as the base URL.** Simplest, but every route is `local`.
2. **Mock HTTP transport** (`http_client=httpx.Client(transport=MockTransport(...))`). Keeps the
   hostname, but the latest `openai` and `anthropic` use `httpx2` and older ones `httpx`, so
   the example would depend on library internals, and each reply would be hand-written.
3. **The fake provider as an HTTP proxy.** The clients use `http://` with the real hostname
   (`http://api.openai.com/v1`, `http://bedrock-runtime.eu-central-1.amazonaws.com`), and
   `HTTP_PROXY` points at the fake provider, which receives the full URL. Works with any client
   version and any cloud client, with no library-specific code.

## Decision
Option 3. `examples/_demo.py` sets the proxy variables for the run and refuses to start if
`urllib.request.getproxies()` doesn't pick them up, so a request can't reach a real provider.
The fake provider now routes by path suffix and accepts a full URL, which adds the Azure OpenAI
deployment paths, Bedrock `invoke` and Vertex `rawPredict`/`streamRawPredict` (the model comes
from the path). A `model` behavior sets the model a reply reports, as Azure reports the model
behind a deployment. `tests/test_examples.py` runs each example and checks its payload,
including that prompts, replies, exception text and the customer's Azure/GCP names are absent.

## Trade-offs
- The examples use `http://`, not `https://`. Gaslit ignores the scheme, so events are the same.
- Bedrock streaming (`invoke-with-response-stream`, an AWS binary event stream) is not served;
  the Bedrock example is non-streaming. Add it if a test needs it.
- The examples need the latest client libraries (Bedrock's `api_key` is recent), so the
  older-version CI jobs don't run them.
