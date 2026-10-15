# Session handoff

Read this at the start of every session, after `CLAUDE.md`. Update it at the end of every
session, in the same PR as the session's other changes, so it reaches `main`.

**Last updated:** 2026-10-15

## Where the project is

- **Done: M0 and M1.** The `gaslit` Python SDK (`sdk/python/`) has `init`, `track`, `flush`,
  `shutdown`, per-minute summaries, sampling, the bounded queue, the background sender, fork and
  exit handling, and debug mode. Privacy, fail-open tests and the overhead benchmark run in CI.
- **Server** (`server/`): FastAPI with `/healthz` and `/readyz` only. No tables, no ingest yet.
- **Deployment:** written but switched off (repo variables `PRODUCTION_ENABLED`, `INFRA_ENABLED`
  unset). Production only, no staging (ADR 0006). Nothing is hosted or paid for until M3 (ADR 0007).
- **Next: M2**, the SDK integrations (below).

## Decisions made recently (details in the ADRs)

| ADR | Decision |
|---|---|
| 0005 | Product name **Gaslit**, `gaslit.dev`. Deploy by CI after tests pass; Terraform manages Cloudflare |
| 0006 | Production only, no staging. Each deploy needs Saad's approval in GitHub |
| 0007 | Database on **Neon** (Free while testing, Frankfurt). One 256 MB Fly machine that scales to zero. No hosting before M3 |
| 0008 | **One shared database** for all users, with per-account limits via `entitlements()` (no per-user databases). **No rollup worker at launch**: the dashboard sums summaries at read time. Includes the multi-tenant checklist for M3 |

## Platform status (outside the repo)

| Item | Status |
|---|---|
| Domain `gaslit.dev` | Bought on Cloudflare Registrar 2026-10-14. DNSSEC was "pending" (Cloudflare adds the DS record itself) |
| Email-spoofing records (null MX, SPF `-all`, DMARC reject) | Done |
| PyPI name `gaslit` | Claimed: placeholder 0.0.0 uploaded 2026-10-14. The upload token was deleted |
| GitHub branch protection on `main` | Done |
| GitHub secret scanning | Saad chose not to enable it |
| Repo visibility | **Public**, which keeps environment approval gates free (ADR 0006). D5 said private; Saad has not changed it |
| Neon, Fly app, Cloudflare tokens, GitHub environments, Terraform, certificates | **Not started.** Runbook steps 2-6 in `docs/deploy.md`, at M3 |
| Google and GitHub OAuth apps | M4 (runbook step 7) |

## Next: M2 plan (small PRs, in order)

1. **Fake provider server** (`tools/fake-provider/`): simulates latency, slow streams, 429, 529,
   5xx and timeouts, so tests and demos cost no tokens (brief §9.11).
2. **Host and route detection** (brief §6.6): OpenAI, Azure, Bedrock, Vertex and
   OpenAI-compatible endpoints, by hostname.
3. **OpenAI integration**: `chat.completions` and `responses`, sync and async, streaming and not.
4. **Anthropic integration**: `messages.create` and `messages.stream`, sync and async.
5. **Examples** (`examples/`) that print what the SDK sends in debug mode.

Before 3 and 4: check the current `openai` and `anthropic` Python SDK internals against their
docs (brief §17). Never capture prompts, outputs, keys or error message text (P1-P7).

## Loose ends

- `sdk/python/src/gaslit/_config.py`: `DEFAULT_ENDPOINT` is still `https://ingest.gaslit.invalid`
  with a stale "until the name is chosen" comment. Change it to `https://ingest.gaslit.dev`
  before the first real release (0.1.0, M7).
- Neon connection strings include `channel_binding=require`; asyncpg forwards it to Postgres,
  which rejects it. Remove it from `DATABASE_URL` (runbook step 2).
- `make test` needs Docker for the server's database test. Cloud sessions have no Docker: run
  `uv run pytest` in `sdk/python`, and in `server` with `-k "not with_database"`.

## Open discussions (not decided; raise with Saad when relevant)

- **Differentiation.** Saad thinks "metadata only" plus official status pages is not enough to
  charge for. Ideas discussed: bring back the cross-user "is it everyone?" signal (dropped in D6)
  once there are a few hundred users; alerts that say whether a spike is likely the provider's
  side; monthly provider-reliability reports usable for SLA claims; privacy as the reason
  regulated companies can use it.
- **Business model.** Competitors (Langfuse $29+/month, LangSmith $39/seat, Helicone $79) use a
  free tier then charge for alerts, retention, teams and volume. Our infrastructure is ~$25/month
  shared once Neon is on the paid plan, so one or two paying users cover it. Paid features stay
  parked until a waitlist shows demand (brief §8).

## How Saad likes to work

- Cost-sensitive: nothing paid unless clearly necessary. Say what something costs before
  suggesting it.
- Explain platform and infrastructure terms plainly; he is learning them and wants to be able to
  explain every part in interviews. Keep answers concise; he asks for detail.
- He merges PRs himself on GitHub and sometimes pushes from VS Code, so fetch `main` before work.
- He wants this handoff file kept current at the end of each session.
