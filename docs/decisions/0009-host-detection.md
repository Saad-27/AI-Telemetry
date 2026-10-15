# 0009: Host detection rules where the brief leaves gaps

- **Status:** accepted
- **Date:** 2026-10-15

## Context
Brief §6.6 maps endpoint hostnames to `provider`, `route` and `endpoint_host`. It leaves a few
cases open, and `endpoint_host` is the one field built from a customer's URL, so a wrong rule
could leak a name (P1, P6). The rules are in `sdk/python/src/gaslit/_hosts.py`.

## Decision
- **Only the hostname is read.** Credentials, port, path, query and fragment are dropped before
  any matching. Suffix rules need a dot before the suffix, so `evilopenai.azure.com` and
  `api.openai.com.evil.example` are `custom`.
- **Bedrock and Vertex take the provider from the model name**, which is read for the match and
  never stored. On Bedrock the vendor is the first or second dot-separated part (so
  `us.anthropic.claude-...` and ARNs work). An unknown or missing vendor is `custom`, with the
  route still `bedrock`. On Vertex, Claude, Llama, Mistral, DeepSeek and `openai/` models map to
  their vendors and everything else is `google`.
- **`aiplatform.googleapis.com`** (no region) is the global endpoint, so `endpoint_region` is
  `global`.
- **Any host on the list that is not OpenAI, Anthropic, Azure, Bedrock, Vertex or Gemini**
  (Groq, Mistral, DeepSeek, OpenRouter, Together, Fireworks, xAI) is route `openai_compatible`,
  because only the OpenAI client reaches them in M2.
- **`api.together.ai` is on the list next to `api.together.xyz`.** Together's current docs use
  `.ai` (checked 2026-10-15).
- **Local means not globally routable**, not only "private": loopback, private, link-local and
  shared (CGNAT, such as Tailscale) addresses, plus `localhost`, `*.local`, `*.internal` and
  `*.localhost`. A public IP address is `custom`. A bare name like `vllm` is `custom`.

## Trade-offs
- Not listed, so `custom`: Azure Government and China (`*.azure.us`, `*.azure.cn`), Bedrock
  VPC endpoints, and the Vertex multi-region `*.rep.googleapis.com` hosts, which Google's docs
  did not confirm. Adding one is a one-line change and a new case in `tests/test_hosts.py`.
- The Bedrock vendor list and the Vertex families will need new names as models appear. A new
  vendor shows up as `custom` or `google` until someone adds it.
- The server must repeat these checks on ingest (brief §6.6) at M3. That is not built yet.
