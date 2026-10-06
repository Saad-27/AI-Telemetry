# 0005: Staging from day one, deployed by CI

- **Status:** accepted
- **Date:** 2026-10-05

## Context
The brief puts Terraform and continuous deployment in M7 (§14). Saad wants deployment set up
while the code is still being written, so each milestone can be tried on a real URL as soon as
it lands, and so deployment problems show up early rather than in launch week.

D1 is also settled here: the product is **Gaslit** (`gaslit.dev`). `gaslit` was unclaimed on
PyPI and npm and the domain was available on 2026-10-05. Renaming the code placeholders
(`yourpkg`, `YOURPKG_KEY`, `rm_live_`) is a separate PR, to avoid clashing with SDK work in
progress. GitHub org and trademark checks are still to do.

## Options considered
1. **Keep the brief's order:** no deploys until M7. Least work now, but the first deploy
   then happens with every component at once, under launch pressure.
2. **Staging now, production dormant until M7.** A small "walking skeleton": today's
   `/healthz` server goes live, and every green merge to `main` redeploys it.
3. **Preview environment per PR.** Nicer review flow, but each needs its own app and
   database. Too much cost and machinery for one developer (§21).

For managing Fly itself:
- **Terraform:** Fly no longer maintains its Terraform provider, so this is not an option.
- **`flyctl` from CI with a `fly.toml` per environment:** Fly's supported path.

For Terraform state:
- **Local file:** CI cannot use it, and losing the laptop loses the state.
- **HCP Terraform:** another account and vendor.
- **Cloudflare R2 bucket via the S3 backend:** we already use Cloudflare, and it is within the
  free tier at this size.

## Decision
Option 2.
- **Fly:** `deploy/fly/staging.toml` and `production.toml`, in Frankfurt. Staging scales to zero
  when idle; production keeps one machine warm. Until M3 splits ingest and web-api, one app
  (`gaslit-api-*`) serves both hostnames.
- **Deploy workflow** (`.github/workflows/deploy.yml`): triggered by `workflow_run` when CI
  succeeds on a push to `main`, so only tested commits deploy and CI is not run twice. It
  deploys staging, then smoke-tests `/readyz`. Production is a second job behind the
  `production` environment's required reviewer, and stays off until `PRODUCTION_ENABLED` is
  set in M7.
- **Terraform** (`infra/terraform/`) manages Cloudflare only: proxied DNS, TLS settings
  (Full strict, HTTPS-only, TLS 1.2+, HSTS), a firewall rule that only allows SDK, health and
  ACME paths on ingest hosts, and per-IP rate limiting for ingest. State is kept in R2. PRs
  get a plan in the run summary; merges apply after approval of the `infra` environment.
- **Secrets** live only in Fly (`fly secrets`) and GitHub (environment or repo secrets).
  `docs/deploy.md` lists every one by name, so the setup can be rebuilt from the repo.

## Trade-offs
- Staging costs a little money from now on rather than from M7. Scale-to-zero keeps it small.
- Plan and apply share one read-write Cloudflare token. A read-only token for PR plans is
  tighter; split them when anyone else can open PRs.
- The firewall and rate-limit rules own their Cloudflare phase. Rules added in the dashboard
  for those phases will be overwritten, so all rules go through Terraform.
- Not verified here because Terraform and flyctl were not available in the authoring
  environment: provider schema details, R2 support for `use_lockfile`, and flyctl flags. CI
  runs `terraform validate`, and the first real run of each workflow is the check (brief §17).
