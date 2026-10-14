# 0006: Production only, no staging

- **Status:** accepted
- **Date:** 2026-10-14

## Context
ADR 0005 stood up a staging environment ahead of M7 so each milestone could be tried on a real
URL. None of the platform setup had been done when Saad reviewed it on 2026-10-14. Staging
means a second Fly app and a second always-on database to pay for and keep in sync, for a
service that so far only answers `/healthz` and `/readyz`, with no users to protect.

## Options considered
1. **Keep staging now, production in M7** (ADR 0005). Catches deploy problems early, but
   pays for a second environment from now on.
2. **Defer staging to M3**, when there is first something worth testing on it. Cheaper, but
   the first real deploy still happens later.
3. **Production only, from now.** One Fly app, one database, one set of hostnames. The
   deploy path is proven now, on the environment that matters.

## Decision
Option 3. `gaslit-api-production` in `fra` is the only hosted environment. Every green merge to
`main` deploys to it after Saad approves the run in the `production` GitHub environment, then
smoke-tests `/readyz`. `app.gaslit.dev` and `ingest.gaslit.dev` point at it via Terraform. The
Cloudflare, Terraform and deploy-workflow design from ADR 0005 is otherwise unchanged.

## Trade-offs
- No place to try a change on real infrastructure before users see it. Until the private alpha
  (M7) there are no users, so this costs nothing yet. Mitigations: CI, local `make dev`, the
  required approval, the `/readyz` smoke test and `fly deploy --image` rollbacks.
- Load tests (M3, brief §20.6) and the first Alembic migrations run against production. That is
  fine while it holds no user data. Revisit before alpha: if migrations or load tests then need
  a safe target, add a scale-to-zero staging app with the config ADR 0005 described.
- "Production" is publicly reachable before launch. It only serves health checks until M3, and
  the brief §11.11 checklist still gates inviting anyone.
