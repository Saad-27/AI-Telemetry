# 0027: Refuse requests that skip Cloudflare, with a secret header

- **Status:** accepted (Saad chose the secret header, 2026-10-21)
- **Date:** 2026-10-21

## Context
Every Fly app also answers at `<app>.fly.dev`. Requests sent there reach the ingest app
directly, around everything Terraform sets up at Cloudflare (brief §11.5): the ingest path
filter, the per-IP rate limit (300 per 10 s) and DDoS protection. The step 7 load test showed
why that matters: 2,000 bad-key requests a second from one client pegged the machine
(`docs/capacity.md`). Clients also cannot be trusted to send a true `CF-Connecting-IP` unless
we know the request came through Cloudflare, so the per-IP limit on invalid keys (ADR 0017)
waits on this too.

## Options considered
1. **Cloudflare adds a secret header; the app refuses requests without it.** Free (request
   header transform rules are on the free plan), one Terraform rule and a small middleware.
   The secret lives in three places (GitHub, Cloudflare, Fly).
2. **Authenticated origin pulls (mTLS).** Cloudflare presents a client certificate. Fly's
   proxy ends TLS and cannot ask for or check client certificates, so the app never sees it.
3. **Allow only Cloudflare's IP ranges.** Fly's proxy sits in front of the app, so the app
   sees Fly's addresses; the ranges also change and would need upkeep.
4. **Cloudflare Tunnel.** Removes the public Fly hostname entirely, but adds a `cloudflared`
   process to every machine, more memory on a 256 MB machine, and a new component (§21).

## Decision
Option 1.
- Terraform (`cloudflare_ruleset.edge_secret`, phase `http_request_late_transform`) sets
  `X-Gaslit-Edge` on every request to our hostnames. The value is the sensitive variable
  `edge_secret` (GitHub secret `EDGE_SECRET`), so plans in public run summaries hide it. At
  least 32 letters or digits; `openssl rand -hex 32` makes one.
- The app (`app/core/edge.py`, pure ASGI, outermost) answers `503 {"error":"edge_check_failed"}`
  (403 until the change below)
  before routing when the header is missing or wrong, compared in constant time. It removes
  the header before the app sees it, so no log or error report can ever hold it. About 2 µs
  per request either way.
- Only `/healthz` stays open: Fly's health checks reach the machine directly. It does no
  database work. `/readyz` is behind the check; the deploy smoke test reaches it through
  `PUBLIC_URL` (Cloudflare).
- On when the `EDGE_SECRET` Fly secret is set, off when it is empty (locally, tests, and
  until the domain goes through Cloudflare). The app refuses to start if a value is under 32
  characters. `EDGE_SECRET` may hold two values separated by a space, so a rotation never
  refuses real traffic (runbook, "Rotate the edge secret").
- ~~403, because the SDK stops sending after a 401 or 403: an SDK pointed at `*.fly.dev` is
  misconfigured and should say so rather than retry.~~ Changed to 503, see below.
- The Infra workflow can be run by hand (`workflow_dispatch`), to re-apply after a rotation.

## Trade-offs
- The secret is in Terraform state (the private R2 bucket) and in Cloudflare's config. Anyone
  who can read either can skip Cloudflare; both already hold more sensitive things.
- Refused requests still cost Fly's proxy and uvicorn's HTTP parsing; a large flood straight at
  `*.fly.dev` can still load the machine, just far less than before. Revisit (Tunnel, option 4)
  if that happens.
- Set on Fly 2026-10-21. Since then the public app (`create_app`) refuses to start on Fly
  without it; the worker, which has no public port, does not need it.

## Change (Saad, 2026-10-21): 503 instead of 403
A wrong or missing secret is far more likely to be our mistake (a rotation, or a Terraform
change that drops the rule; a mismatch already happened once while setting it up) than an SDK
pointed at `*.fly.dev`, which the default endpoint never is. After a 403 every SDK would stop
sending until its process restarts, so a mismatch would silence all users even after we fixed
it. After a 503 the SDK retries (and drops a batch after three tries), logs that the fault is on
Gaslit's side, and resumes once fixed. The answer carries `Retry-After: 5`. An SDK that really is
pointed at `*.fly.dev` now retries instead of stopping; that costs refused requests only.
