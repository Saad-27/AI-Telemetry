# Gaslit: deployment plan and setup PR

> **Written:** 5 October 2026, in a planning session with Saad.
> **Purpose:** gives the coding agent everything it needs to open the deployment PR, and
> explains to Saad what gets built at each stage and why.
> **This file is a delivery vehicle. Do not commit it.** Everything worth keeping is in the
> files it creates, especially `docs/deploy.md` and ADR 0004.

---

## Part 1: Instructions for the agent

1. Read `CLAUDE.md` and `PROJECT_BRIEF.md` as usual. This PR changes the order in which the
   brief (§14) delivers deployment, and ADR 0004 records that change. It does not change any
   privacy or security rule.
2. Create a branch from the latest `main`: `deploy/staging-skeleton`.
3. Create every file in **Part 5** with exactly the contents shown. Paths are relative to the
   repo root.
4. Apply the edits in **Part 6** to the existing files.
5. **Do not touch** `sdk/` or `server/` code. Do **not** rename the placeholders (`yourpkg`
   etc.) in this PR. That is the next PR (see the rollout table).
6. Check your work:
   - `actionlint .github/workflows/*.yml`, if actionlint is installed.
   - `terraform fmt -check -recursive infra/terraform`, then
     `terraform -chdir=infra/terraform init -backend=false` and
     `terraform -chdir=infra/terraform validate`, if Terraform is installed. Fix anything
     these report, and say so in the PR.
   - `make lint test` must still pass. Nothing here touches Python, so it should.
7. Commit using the message in **Part 7** and open a PR with the description in **Part 7**.
8. Delete this file from the working tree. Do not commit it.

Nothing goes live when this merges. Each workflow stays switched off until Saad sets the
repository variables `STAGING_ENABLED` and `INFRA_ENABLED`, after the platform setup in
`docs/deploy.md`.

---

## Part 2: What this is and why

**Decision:** stand up **staging now** instead of waiting for M7. Today's `/healthz` server
goes live in Frankfurt, and every merge to `main` that passes CI redeploys it automatically.
Production waits until M7, behind a manual approval.

**Why:**
- Each milestone can be tried on a real URL the day it lands.
- Deployment problems (secrets, TLS, DNS, database networking) get solved now, while there is
  almost nothing to deploy. They never get mixed up with code bugs, or left until launch week.
- It fills the brief's CI/CD and infrastructure-as-code CV gaps early, with real history behind
  them.

**Name (D1):** **Gaslit**, at `gaslit.dev`. *"Status page says All Systems Operational. Your
p95 says otherwise."* On 5 Oct, `gaslit` was unclaimed on PyPI and npm and `gaslit.dev` was
available. A GitHub org and a trademark search are still to do.

**What lives where:**
- **In the repo:** config and code. Fly app configs, GitHub Actions workflows, Terraform for
  Cloudflare, the ADR and the runbook.
- **On the platforms:** secrets and account settings. Fly secrets, GitHub environment
  secrets, Cloudflare tokens, the OAuth apps.

`docs/deploy.md` lists every platform-side item by name, so the whole setup can be rebuilt
from the repo.

**How a change reaches users:**

```
PR ──► CI ──► merge to main ──► CI passes ──► Deploy workflow ──► staging ──► smoke test /readyz
                                                                     │
                                          (M7) Saad approves ────────┴──► production
infra/terraform/** changes: PR shows a Terraform plan ──► merge ──► Saad approves ──► apply
```

---

## Part 3: What gets made at each stage, and when you can test

This is the same as the "Rollout by milestone" and "When you can test" sections of
`docs/deploy.md` (Part 5), which is where they live permanently.

### Rollout by milestone

| When | What gets built (code) | What Saad does (platforms) | Why then |
|---|---|---|---|
| **Now (during M1)** | This PR: Fly configs, deploy and infra workflows, Terraform for Cloudflare, ADR 0004, runbook | `docs/deploy.md` steps 0-6: MFA, buy the domain, Fly app and database, Cloudflare R2 and token, GitHub environments and variables, first Terraform run, certificates | Proves the path to a live URL while there is almost nothing to deploy |
| **Next PR** | Rename the placeholders: `yourpkg` → `gaslit`, `YOURPKG_KEY` → `GASLIT_KEY`, `rm_live_` → `gl_live_`, `<domain>` → `gaslit.dev` | Nothing | Must happen before the first SDK release, because env var names and key prefixes are hard to change once installed in users' apps |
| **M2** | Nothing new for deployment | Nothing | SDK work runs in users' apps, not on our servers |
| **M3** | Turn on the Alembic `release_command` in both Fly configs. Add separate `ingest` and `worker` Fly apps (the worker has no public port) and point `staging-ingest` at the ingest app. Load test against staging, with results in `docs/capacity.md` | Create the new Fly apps, attach the database, add their deploy tokens | First real schema and first real traffic. Ingest needs to scale separately from the dashboard (brief §20) |
| **M4** | Web SPA in the deploy, OAuth and session-key settings, security headers | Create the OAuth apps (step 7) and add them with `fly secrets set` | Login and the dashboard need them. Staging becomes a clickable website |
| **M5** | Status poller inside the worker | Nothing | Outbound requests only |
| **M7** | Production apps, `release.yml` for PyPI Trusted Publishing, base images pinned by digest, uptime check, Grafana | Production Fly apps and database, production OAuth apps, the `app` and `ingest` hosts in tfvars, `PRODUCTION_ENABLED=true`, a tested backup restore, PyPI (step 8) | Launch: the brief's §11.11 checklist must be fully ticked |

### When Saad can test

| From | What | How |
|---|---|---|
| Now | SDK unit tests | `make test` |
| End of M1 | Exactly what the SDK sends | `init(debug=True)` prints every payload locally. No account needed |
| End of M2 | Real OpenAI and Anthropic calls | Own scripts with debug output, or the fake provider (no token cost) |
| End of M3 | Full pipeline: app → ingest → database → rollups | Locally with `make dev`; on staging if steps 0-6 are done |
| End of M4 | **The real website:** sign up, create a key, see calls on the dashboard | `https://staging-app.gaslit.dev` |
| M7 | Production with real users (private alpha) | `https://app.gaslit.dev` |

---

## Part 4: Known unverified items (flag these in the PR)

These were written without Terraform or flyctl available, using the Cloudflare provider v5.27
docs and Fly's docs as of October 2026. The first real runs are the check (brief §17).

- **Cloudflare provider v5:** the schema details of `cloudflare_zone_setting` (the
  `security_header` object) and `cloudflare_ruleset` (the `ratelimit` block). CI's
  `terraform validate` catches most of this.
- **R2 and `use_lockfile`:** whether R2 accepts Terraform's lock file. If `init` or `apply`
  fails on it, remove the line (ADR 0004).
- **API token permissions:** whether the Cloudflare token needs a permission beyond DNS Edit,
  Zone Settings Edit and Zone WAF Edit to manage rulesets.
- **`flyctl deploy` with a directory argument plus an absolute `--config` path:** this is
  meant to build from `server/` using `server/Dockerfile`.
- **TXT record quoting:** whether `_fly-ownership` TXT values show a permanent diff because
  Cloudflare adds quotes.
- **Rate-limit limits:** the free-plan rule assumes a 10 s period, a 10 s block, and
  characteristics `ip.src` plus `cf.colo.id`.

---

## Part 5: Files to create

### 5.1 `deploy/fly/staging.toml`

Fly config for staging: Frankfurt, health check on `/healthz`, scales to zero when idle so it costs almost nothing between tests.

~~~~toml
# Staging. Deployed automatically after CI passes on main (.github/workflows/deploy.yml).
# Until M3 splits ingest and web-api into separate apps, this one app serves both
# hostnames. Secrets (DATABASE_URL, ...) are never in this file: set them with
# `fly secrets set -a gaslit-api-staging`. See docs/deploy.md.

app = "gaslit-api-staging"
primary_region = "fra" # D12: EU-hosted

[build]
  # Relative to the directory passed to `fly deploy` (server/), which is also the build context.
  dockerfile = "Dockerfile"

# Added in M3, when Alembic exists: migrations run once per deploy, before new machines start.
# [deploy]
#   release_command = "alembic upgrade head"

[http_service]
  internal_port = 8000
  force_https = true
  # Staging scales to zero when idle, so it costs almost nothing between tests.
  auto_stop_machines = "stop"
  auto_start_machines = true
  min_machines_running = 0

  [http_service.concurrency]
    type = "requests"
    soft_limit = 200
    hard_limit = 250

  # Liveness only. /readyz (database reachable) is checked by the deploy smoke test instead,
  # so a database blip does not make Fly restart healthy machines.
  [[http_service.checks]]
    grace_period = "10s"
    interval = "15s"
    timeout = "3s"
    method = "GET"
    path = "/healthz"

[[vm]]
  size = "shared-cpu-1x"
  memory = "256mb"
~~~~

### 5.2 `deploy/fly/production.toml`

Fly config for production (dormant until M7): keeps one machine warm, more memory.

~~~~toml
# Production. Deployed only after staging succeeds AND you approve it in GitHub
# (the `production` environment). Dormant until the PRODUCTION_ENABLED repo variable is
# "true" (M7). Same image as staging; only sizing and idle behaviour differ.

app = "gaslit-api-production"
primary_region = "fra" # D12: EU-hosted

[build]
  dockerfile = "Dockerfile"

# Added in M3, when Alembic exists.
# [deploy]
#   release_command = "alembic upgrade head"

[http_service]
  internal_port = 8000
  force_https = true
  # Always keep one machine warm so the first dashboard visit or SDK batch is not slowed by a
  # cold start. `fly deploy` creates a second machine by default for availability.
  auto_stop_machines = "stop"
  auto_start_machines = true
  min_machines_running = 1

  [http_service.concurrency]
    type = "requests"
    soft_limit = 200
    hard_limit = 250

  [[http_service.checks]]
    grace_period = "10s"
    interval = "15s"
    timeout = "3s"
    method = "GET"
    path = "/healthz"

[[vm]]
  size = "shared-cpu-1x"
  memory = "512mb"
~~~~

### 5.3 `.github/workflows/deploy.yml`

Deploys staging after CI passes on a push to `main`, then smoke-tests `/readyz`. The production job needs Saad's approval and `PRODUCTION_ENABLED`. Uses `workflow_run` so only tested commits deploy and CI is not run twice.

~~~~yaml
name: Deploy

# Runs only after the CI workflow passes for a push to main, so every deployed commit has
# been linted and tested. ADR 0004 explains the flow; docs/deploy.md lists the settings it needs.
on:
  workflow_run:
    workflows: [CI]
    types: [completed]
    branches: [main]
  workflow_dispatch: # manual redeploy of main

permissions:
  contents: read

# One deploy at a time, and never cancel one halfway through.
concurrency:
  group: deploy
  cancel-in-progress: false

jobs:
  staging:
    # Off until the STAGING_ENABLED repo variable is "true" (after docs/deploy.md setup).
    # workflow_run fires for pull requests too, and `branches` matches a fork's branch named
    # "main", so also require a push from this repository.
    if: >-
      vars.STAGING_ENABLED == 'true' && (
      (github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main') ||
      (github.event.workflow_run.conclusion == 'success' &&
       github.event.workflow_run.event == 'push' &&
       github.event.workflow_run.head_repository.full_name == github.repository))
    runs-on: ubuntu-latest
    environment:
      name: staging
      url: ${{ vars.PUBLIC_URL }}
    env:
      SHA: ${{ github.event.workflow_run.head_sha || github.sha }}
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          ref: ${{ env.SHA }}
          persist-credentials: false
      - uses: superfly/flyctl-actions/setup-flyctl@ed8efb33836e8b2096c7fd3ba1c8afe303ebbff1 # v1.6
      - name: Deploy
        # --ha=false: one machine is enough for staging.
        run: >-
          flyctl deploy server
          --config "$GITHUB_WORKSPACE/deploy/fly/staging.toml"
          --remote-only --ha=false
          --image-label "git-${SHA::12}"
        env:
          FLY_API_TOKEN: ${{ secrets.FLY_API_TOKEN }}
      - name: Smoke test
        # /readyz also proves the app can reach its database.
        run: curl --fail --silent --show-error --retry 10 --retry-delay 3 --retry-all-errors "$PUBLIC_URL/readyz"
        env:
          PUBLIC_URL: ${{ vars.PUBLIC_URL }}

  production:
    needs: staging
    # Dormant until M7. Once enabled, the `production` environment's required reviewer
    # (you) must approve each run in the Actions tab before it starts.
    if: vars.PRODUCTION_ENABLED == 'true'
    runs-on: ubuntu-latest
    environment:
      name: production
      url: ${{ vars.PUBLIC_URL }}
    env:
      SHA: ${{ github.event.workflow_run.head_sha || github.sha }}
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          ref: ${{ env.SHA }}
          persist-credentials: false
      - uses: superfly/flyctl-actions/setup-flyctl@ed8efb33836e8b2096c7fd3ba1c8afe303ebbff1 # v1.6
      - name: Deploy
        run: >-
          flyctl deploy server
          --config "$GITHUB_WORKSPACE/deploy/fly/production.toml"
          --remote-only
          --image-label "git-${SHA::12}"
        env:
          FLY_API_TOKEN: ${{ secrets.FLY_API_TOKEN }}
      - name: Smoke test
        run: curl --fail --silent --show-error --retry 10 --retry-delay 3 --retry-all-errors "$PUBLIC_URL/readyz"
        env:
          PUBLIC_URL: ${{ vars.PUBLIC_URL }}
~~~~

### 5.4 `.github/workflows/infra.yml`

Terraform CI: format, validate and plan on PRs (plan in the run summary); apply on `main` after approval of the `infra` environment.

~~~~yaml
name: Infra

# Terraform for Cloudflare (infra/terraform). PRs: format, validate and plan (the plan shows
# in the run summary). Merges to main: apply, after you approve the `infra` environment.
# Plan and apply stay off until the INFRA_ENABLED repo variable is "true" (docs/deploy.md).
on:
  pull_request:
    paths: [infra/terraform/**, .github/workflows/infra.yml]
  push:
    branches: [main]
    paths: [infra/terraform/**, .github/workflows/infra.yml]

permissions:
  contents: read

# Never run two Terraform operations against the same state at once.
concurrency:
  group: infra
  cancel-in-progress: false

defaults:
  run:
    shell: bash # explicit, so pipelines fail if any command fails (pipefail)
    working-directory: infra/terraform

env:
  TF_IN_AUTOMATION: "1"
  TF_INPUT: "0"

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: hashicorp/setup-terraform@dfe3c3f87815947d99a8997f908cb6525fc44e9e # v4.0.1
        with:
          terraform_version: 1.x
          terraform_wrapper: false
      - run: terraform fmt -check -recursive -diff
      - run: terraform init -backend=false
      - run: terraform validate

  plan:
    needs: check
    # Secrets are not available to PRs from forks, so only plan PRs from this repository.
    if: >-
      vars.INFRA_ENABLED == 'true' &&
      github.event_name == 'pull_request' &&
      github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    env:
      CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}
      AWS_ACCESS_KEY_ID: ${{ secrets.R2_ACCESS_KEY_ID }}
      AWS_SECRET_ACCESS_KEY: ${{ secrets.R2_SECRET_ACCESS_KEY }}
      AWS_ENDPOINT_URL_S3: ${{ vars.R2_ENDPOINT }}
      TF_VAR_zone_id: ${{ vars.CLOUDFLARE_ZONE_ID }}
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: hashicorp/setup-terraform@dfe3c3f87815947d99a8997f908cb6525fc44e9e # v4.0.1
        with:
          terraform_version: 1.x
          terraform_wrapper: false
      - run: terraform init
      - name: Plan
        # -lock=false: a plan only reads state. Output goes to the run summary.
        run: |
          terraform plan -lock=false -no-color | tee plan.txt
          { echo '### Terraform plan'; echo '```'; cat plan.txt; echo '```'; } >> "$GITHUB_STEP_SUMMARY"

  apply:
    needs: check
    if: vars.INFRA_ENABLED == 'true' && github.event_name == 'push' && github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    environment: infra # required reviewer: you approve before it runs
    env:
      CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}
      AWS_ACCESS_KEY_ID: ${{ secrets.R2_ACCESS_KEY_ID }}
      AWS_SECRET_ACCESS_KEY: ${{ secrets.R2_SECRET_ACCESS_KEY }}
      AWS_ENDPOINT_URL_S3: ${{ vars.R2_ENDPOINT }}
      TF_VAR_zone_id: ${{ vars.CLOUDFLARE_ZONE_ID }}
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: hashicorp/setup-terraform@dfe3c3f87815947d99a8997f908cb6525fc44e9e # v4.0.1
        with:
          terraform_version: 1.x
          terraform_wrapper: false
      - run: terraform init
      - run: terraform plan -no-color -out=tfplan
      - run: terraform apply -no-color tfplan
~~~~

### 5.5 `infra/terraform/versions.tf`

Provider pin and remote state in a Cloudflare R2 bucket, so state is not on a laptop and CI can use it.

~~~~hcl
terraform {
  required_version = ">= 1.10"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.27"
    }
  }

  # State lives in a Cloudflare R2 bucket (S3-compatible), so it is not on anyone's laptop
  # and CI can use it. Credentials and the endpoint come from the environment:
  #   AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY  (an R2 API token for this bucket only)
  #   AWS_ENDPOINT_URL_S3=https://<account_id>.r2.cloudflarestorage.com
  backend "s3" {
    bucket = "gaslit-tfstate"
    key    = "cloudflare/terraform.tfstate"
    region = "auto"

    # R2 is not AWS: skip the AWS-only checks.
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
    skip_s3_checksum            = true
    use_path_style              = true

    # State locking via a lock object (Terraform 1.10+). Verify R2 accepts it on first
    # `terraform init`; if not, remove this line and rely on the CI concurrency group.
    use_lockfile = true
  }
}

# Reads CLOUDFLARE_API_TOKEN from the environment. The token never goes in a file.
provider "cloudflare" {}
~~~~

### 5.6 `infra/terraform/variables.tf`

Inputs: the zone ID (from CI), the domain, and the hostname → Fly app map.

~~~~hcl
variable "zone_id" {
  description = "Cloudflare zone ID for the domain (Cloudflare dashboard > domain > Overview). Set via TF_VAR_zone_id."
  type        = string
}

variable "domain" {
  description = "Apex domain (D1)."
  type        = string
  default     = "gaslit.dev"
}

variable "hosts" {
  description = <<-EOT
    Subdomain => where it points. `fly_app` is the Fly app it routes to; `role` is "app"
    (dashboard) or "ingest" (SDK traffic, which gets stricter firewall rules).
    `fly_ownership` is the value of the _fly-ownership TXT record that `fly certs setup`
    prints; Fly needs it to issue a certificate behind Cloudflare's proxy.
  EOT
  type = map(object({
    fly_app       = string
    role          = string
    fly_ownership = optional(string)
  }))

  validation {
    condition     = alltrue([for h in values(var.hosts) : contains(["app", "ingest"], h.role)])
    error_message = "Each host's role must be \"app\" or \"ingest\"."
  }
}

variable "ingest_rate_limit_per_10s" {
  description = "Requests per 10 s per client IP on ingest hosts before Cloudflare blocks for 10 s. An SDK process sends ~2 requests per 10 s, so 300 allows ~150 processes behind one NAT IP."
  type        = number
  default     = 300
}
~~~~

### 5.7 `infra/terraform/terraform.tfvars`

Non-secret settings, reviewed in PRs. Staging hosts now; production hosts commented out until M7.

~~~~hcl
# Non-secret settings, reviewed in PRs. zone_id comes from the TF_VAR_zone_id environment
# variable and the API token from CLOUDFLARE_API_TOKEN (docs/deploy.md).

# Universal SSL on Cloudflare's free plan covers one subdomain level (*.gaslit.dev), so
# staging uses "staging-app" rather than "app.staging".
hosts = {
  "staging-app" = {
    fly_app = "gaslit-api-staging"
    role    = "app"
    # fly_ownership = "<value from `fly certs setup staging-app.gaslit.dev -a gaslit-api-staging`>"
  }
  "staging-ingest" = {
    fly_app = "gaslit-api-staging"
    role    = "ingest"
    # fly_ownership = "<value from `fly certs setup staging-ingest.gaslit.dev -a gaslit-api-staging`>"
  }

  # Production (M7):
  # "app"    = { fly_app = "gaslit-api-production", role = "app" }
  # "ingest" = { fly_app = "gaslit-api-production", role = "ingest" }
}
~~~~

### 5.8 `infra/terraform/main.tf`

Proxied DNS to Fly; TLS (Full strict, HTTPS only, TLS 1.2+, HSTS); a firewall rule that only allows SDK, health and ACME paths on ingest hosts; per-IP rate limiting on ingest.

~~~~hcl
locals {
  ingest_hosts = sort([for name, h in var.hosts : "${name}.${var.domain}" if h.role == "ingest"])
  # Cloudflare rule expressions take a space-separated set of quoted strings.
  ingest_host_set = join(" ", [for h in local.ingest_hosts : "\"${h}\""])
}

# ---- DNS -------------------------------------------------------------------------------

# Each hostname is a proxied CNAME to its Fly app, so traffic passes through Cloudflare's
# TLS, WAF and DDoS protection before reaching Fly.
resource "cloudflare_dns_record" "host" {
  for_each = var.hosts

  zone_id = var.zone_id
  name    = "${each.key}.${var.domain}"
  type    = "CNAME"
  content = "${each.value.fly_app}.fly.dev"
  proxied = true
  ttl     = 1 # automatic; required for proxied records
  comment = "Fly app ${each.value.fly_app} (managed by Terraform)"
}

# Proves domain ownership to Fly so it can issue the origin certificate behind the proxy.
resource "cloudflare_dns_record" "fly_ownership" {
  for_each = { for name, h in var.hosts : name => h if h.fly_ownership != null }

  zone_id = var.zone_id
  name    = "_fly-ownership.${each.key}.${var.domain}"
  type    = "TXT"
  content = each.value.fly_ownership
  ttl     = 300
  comment = "Fly certificate ownership (managed by Terraform)"
}

# ---- TLS (brief §11.2) -----------------------------------------------------------------

# Full (strict): Cloudflare -> Fly is HTTPS with a valid certificate. "Flexible" would send
# traffic to Fly unencrypted and can cause redirect loops.
resource "cloudflare_zone_setting" "ssl" {
  zone_id    = var.zone_id
  setting_id = "ssl"
  value      = "strict"
}

resource "cloudflare_zone_setting" "always_use_https" {
  zone_id    = var.zone_id
  setting_id = "always_use_https"
  value      = "on"
}

resource "cloudflare_zone_setting" "min_tls_version" {
  zone_id    = var.zone_id
  setting_id = "min_tls_version"
  value      = "1.2"
}

# HSTS, as in the brief. Browsers already force HTTPS for every .dev domain (the TLD is
# preloaded), so this mainly matters for non-browser clients and if the domain ever changes.
resource "cloudflare_zone_setting" "security_header" {
  zone_id    = var.zone_id
  setting_id = "security_header"
  value = {
    strict_transport_security = {
      enabled            = true
      max_age            = 31536000
      include_subdomains = true
      preload            = false
      nosniff            = true
    }
  }
}

# ---- Firewall (brief §11.5) --------------------------------------------------------------

# Ingest hosts only serve the SDK. Anything else (scanners probing /wp-admin, /.env, the
# dashboard API) is blocked at Cloudflare and never reaches our machines.
# Let's Encrypt's HTTP-01 path stays open so Fly can renew certificates.
resource "cloudflare_ruleset" "firewall" {
  count = length(local.ingest_hosts) > 0 ? 1 : 0

  zone_id = var.zone_id
  name    = "Custom firewall rules"
  kind    = "zone"
  phase   = "http_request_firewall_custom"

  rules = [
    {
      ref         = "ingest_paths_only"
      description = "Ingest hosts: only the SDK, health and ACME paths"
      action      = "block"
      expression  = join(" ", [
        "http.host in {${local.ingest_host_set}} and not (",
        "http.request.uri.path in {\"/v1/ingest\" \"/healthz\" \"/readyz\"}",
        "or starts_with(http.request.uri.path, \"/.well-known/acme-challenge/\"))",
      ])
    },
  ]
}

# Coarse per-IP flood protection for ingest. Per-key limits and quotas are enforced in the
# app; this only stops one IP hammering us before the app sees it. The free plan allows one
# rate-limit rule with a 10 s period and 10 s block, keyed on IP and Cloudflare data centre.
resource "cloudflare_ruleset" "rate_limit" {
  count = length(local.ingest_hosts) > 0 ? 1 : 0

  zone_id = var.zone_id
  name    = "Rate limits"
  kind    = "zone"
  phase   = "http_ratelimit"

  rules = [
    {
      ref         = "ingest_per_ip"
      description = "Ingest: per-IP flood protection"
      action      = "block"
      expression  = "http.host in {${local.ingest_host_set}}"
      ratelimit = {
        characteristics     = ["ip.src", "cf.colo.id"]
        period              = 10
        requests_per_period = var.ingest_rate_limit_per_10s
        mitigation_timeout  = 10
      }
    },
  ]
}
~~~~

### 5.9 `infra/terraform/outputs.tf`

Lists the hostnames to add with `fly certs add`.

~~~~hcl
output "hostnames" {
  description = "Public hostnames managed here. Run `fly certs add <hostname> -a <app>` for each."
  value       = { for name, h in var.hosts : "${name}.${var.domain}" => h.fly_app }
}
~~~~

### 5.10 `docs/decisions/0004-deployment.md`

ADR: why staging comes now, why `flyctl` instead of Terraform for Fly, why the state lives in R2, and the trade-offs.

~~~~markdown
# 0004: Staging from day one, deployed by CI

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
~~~~

### 5.11 `docs/deploy.md`

The permanent runbook: rollout by milestone, when to test, every platform setup step, the inventory of secrets, and common operations.

~~~~markdown
# Deployment runbook

How Gaslit gets from `main` to a public URL, and every platform setting that lives outside
this repo. Why it works this way: [ADR 0004](decisions/0004-deployment.md).

**Rule:** config lives in the repo; secrets and account settings live on the platforms. This
file lists every one of them by name, so the whole setup can be rebuilt from scratch.

## How a change goes live

```
PR ──► CI (lint, tests, docker build) ──► merge to main
                                              │
                         CI passes on main ───┤
                                              ▼
                     Deploy workflow: flyctl deploy ──► staging ──► smoke test /readyz
                                                                        │
                                       (M7) you approve in GitHub ──────┴──► production
```

Changes under `infra/terraform/` take a separate path: the PR shows a Terraform plan, and
merging applies it after you approve.

| Environment | Fly app | URL | Deploys |
|---|---|---|---|
| local | none | `http://127.0.0.1:8000` | `make dev` |
| staging | `gaslit-api-staging` | `https://staging-app.gaslit.dev` (until DNS is ready: `https://gaslit-api-staging.fly.dev`) | every green merge to `main` |
| production (M7) | `gaslit-api-production` | `https://app.gaslit.dev` | after staging, with your approval |

## Rollout by milestone

Deployment grows with the code, so each piece arrives when the milestone that needs it lands.

| When | What gets built (code) | What you do (platforms) | Why then |
|---|---|---|---|
| **Now (during M1)** | Fly configs, deploy and infra workflows, Terraform for Cloudflare, ADR 0004, this runbook | Setup steps 0-6 below: MFA, domain, Fly app and database, Cloudflare, GitHub settings, first Terraform run, certificates | Proves the path to a live URL while there is almost nothing to deploy, so deploy problems never get mixed up with code problems |
| **Right after** | Rename the placeholders (`yourpkg` → `gaslit`, `YOURPKG_KEY` → `GASLIT_KEY`, `rm_live_` → `gl_live_`, `<domain>` → `gaslit.dev`) | Nothing | Must happen before the first SDK release, because env var names and key prefixes are hard to change once installed in users' apps |
| **M2** | Nothing new for deployment. Staging redeploys on every merge | Nothing | SDK work runs in users' apps, not on our servers |
| **M3** | Alembic `release_command` turned on in both Fly configs. Separate Fly apps for `ingest` and `worker` (the worker has no public port), with `staging-ingest` pointed at the ingest app in `terraform.tfvars`. Load test against staging, with results in `docs/capacity.md` | Create the new Fly apps, attach the database and add their deploy tokens | First real database schema and first real traffic. Splitting ingest lets it scale separately from the dashboard (brief §20) |
| **M4** | Web SPA build in the deploy, OAuth and session-key settings, security headers | Step 7 (OAuth apps), then `fly secrets set` for the client IDs, secrets and session key | Login and the dashboard need them. Staging becomes a clickable website |
| **M5** | Status poller inside the worker | Nothing | Outbound requests only, so no infrastructure change |
| **M7** | Production apps, `release.yml` for PyPI Trusted Publishing, base images pinned by digest, uptime check, Grafana | Production Fly apps and database, production OAuth apps, `app`/`ingest` hosts in `terraform.tfvars`, `PRODUCTION_ENABLED=true`, a tested backup restore, step 8 | Launch: the security checklist in brief §11.11 must be fully ticked |

## When you can test

| From | What | How |
|---|---|---|
| Now | SDK unit tests | `make test` |
| End of M1 | Exactly what the SDK sends | `gaslit.init(debug=True)` prints every payload locally, no account needed |
| End of M2 | Real OpenAI and Anthropic calls | Your own scripts with debug output, or the fake provider (no token cost) |
| End of M3 | Full pipeline: app → ingest → database → rollups | Locally with `make dev`; on staging if steps 0-6 are done |
| End of M4 | The real website: sign up, create a key, see calls on the dashboard | `https://staging-app.gaslit.dev` |
| M7 | Production with real users (private alpha) | `https://app.gaslit.dev` |

## One-time setup

Work through these in order. Each step says where the result goes.

### 0. Two-factor login (MFA) everywhere
Turn it on for GitHub, Fly.io, Cloudflare, PyPI and your email before creating anything else
(brief §11.8).

### 1. Domain
1. Buy `gaslit.dev`. Cloudflare Registrar is simplest, because it sells at cost and adds the
   domain to Cloudflare automatically.
2. Cloudflare dashboard > the domain > **DNS > Settings**: enable **DNSSEC**. Check that the
   registrar lock is on.
3. Copy the **Zone ID** from the domain's Overview page. It goes into GitHub (step 4).

### 2. Fly.io (staging)
Install `flyctl` and run `fly auth login`, then:

```bash
# App names are global on Fly. If one is taken, choose another and update
# deploy/fly/staging.toml and infra/terraform/terraform.tfvars to match.
fly apps create gaslit-api-staging

# Managed Postgres in Frankfurt, same major version as local dev. Running it without
# --plan lists the plans and prices; pick the smallest.
fly mpg create --name gaslit-staging --region fra --pg-major-version 17

# Stores the connection string as the app secret DATABASE_URL. Nothing goes in the repo.
fly mpg attach <CLUSTER_ID> -a gaslit-api-staging

# Deploy token scoped to this one app, valid for a year. Put a rotation reminder in your calendar.
fly tokens create deploy -a gaslit-api-staging -x 8760h
```

The token is a GitHub secret (step 4). Optional first manual deploy, from the repo root:

```bash
fly deploy server --config "$PWD/deploy/fly/staging.toml" --ha=false
curl https://gaslit-api-staging.fly.dev/readyz   # {"status":"ok"}
```

### 3. Cloudflare (Terraform access)
1. **State bucket:** R2 > Create bucket `gaslit-tfstate`, with location hint Western Europe.
2. **State credentials:** R2 > Manage API tokens > Create: **Object Read & Write**, limited to
   the `gaslit-tfstate` bucket. This gives an Access Key ID and Secret Access Key. The
   endpoint is `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`.
3. **Terraform's API token:** My Profile > API Tokens > Create custom token, with these
   permissions:
   - Zone > DNS > Edit
   - Zone > Zone Settings > Edit
   - Zone > Zone WAF > Edit

   Zone resources: Include > Specific zone > `gaslit.dev`. If an apply fails with a permission
   error on a ruleset, add the permission it names.
4. If the zone already has custom firewall or rate-limiting rules made in the dashboard,
   delete them. Terraform owns those phases and the first apply would fail.

### 4. GitHub repository settings

**Environments** (Settings > Environments):

| Environment | Protection | Secrets | Variables |
|---|---|---|---|
| `staging` | Deployment branches: `main` only | `FLY_API_TOKEN` (from step 2) | `PUBLIC_URL` = `https://gaslit-api-staging.fly.dev` |
| `infra` | Required reviewer: you. Branches: `main` | none | none |
| `production` (M7) | Required reviewer: you. Branches: `main` | `FLY_API_TOKEN` for the production app | `PUBLIC_URL` = `https://app.gaslit.dev` |

**Repository secrets** (Settings > Secrets and variables > Actions > Secrets):
`CLOUDFLARE_API_TOKEN`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` (from step 3).

**Repository variables** (same page > Variables):

| Variable | Value | Effect |
|---|---|---|
| `CLOUDFLARE_ZONE_ID` | from step 1 | Terraform's `zone_id` |
| `R2_ENDPOINT` | `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` | Terraform state location |
| `STAGING_ENABLED` | `true` once steps 2 and 4 are done | turns on staging deploys |
| `INFRA_ENABLED` | `true` once step 5 is done | turns on Terraform plan and apply |
| `PRODUCTION_ENABLED` | `true` in M7 | turns on the production job |

**Other settings:**
- **Branch protection on `main`:** require a PR, require the CI checks to pass, block force pushes.
- **Code security:** enable secret scanning and push protection.

### 5. First Terraform run (locally, once)

```bash
cd infra/terraform
export CLOUDFLARE_API_TOKEN=...          # step 3.3
export AWS_ACCESS_KEY_ID=...             # step 3.2 (R2 credentials)
export AWS_SECRET_ACCESS_KEY=...
export AWS_ENDPOINT_URL_S3=https://<ACCOUNT_ID>.r2.cloudflarestorage.com
export TF_VAR_zone_id=...                # step 1.3
terraform init
terraform providers lock -platform=linux_amd64 -platform=darwin_arm64 -platform=windows_amd64
terraform plan
```

Commit the generated `.terraform.lock.hcl` in a PR. It pins the exact provider build CI
uses. If `init` rejects `use_lockfile`, remove that line from `versions.tf` (ADR 0004).
Then set `INFRA_ENABLED=true`. From now on, merges apply the changes after you approve them.

### 6. Custom domain on staging

```bash
fly certs add staging-app.gaslit.dev    -a gaslit-api-staging
fly certs add staging-ingest.gaslit.dev -a gaslit-api-staging
fly certs setup staging-app.gaslit.dev  -a gaslit-api-staging   # prints the _fly-ownership value
```

1. Put each `_fly-ownership` value into `infra/terraform/terraform.tfvars`, then open a PR, merge it and approve the apply.
2. Run `fly certs check staging-app.gaslit.dev -a gaslit-api-staging` until the certificate is issued.
3. Change the `staging` environment's `PUBLIC_URL` to `https://staging-app.gaslit.dev`.

### 7. OAuth apps (needed in M4; can be done now)
The callback path is `/auth/{provider}/callback` (brief §9.3). The secret names are fixed in M4.

- **GitHub:** Settings > Developer settings > OAuth Apps. A GitHub OAuth app allows only one
  callback URL, so create two:
  - `Gaslit (dev)`, with callback `http://localhost:8000/auth/github/callback`
  - `Gaslit (staging)`, with callback `https://staging-app.gaslit.dev/auth/github/callback`
- **Google:** Cloud Console > APIs & Services.
  1. Set up the OAuth consent screen: External, scopes `openid email profile`.
  2. Go to Credentials > OAuth client ID > Web application, and add both callback URLs above
     with `google` in place of `github`.
- **Where the secrets go:** dev secrets in your local `.env`, which is git-ignored. Staging
  secrets via `fly secrets set ... -a gaslit-api-staging`.

### 8. PyPI and npm names
A PyPI "pending publisher" sets up Trusted Publishing, but it does **not** reserve the name.
1. Create a PyPI account with MFA.
2. Add a pending publisher with these values: project `gaslit`, owner `Saad-27`, repo
   `AI-Telemetry`, workflow `release.yml`, environment `pypi`.
3. Publish a placeholder release early (brief §11.7). Do the same on npm when the TypeScript
   SDK starts.

## Inventory: everything that lives outside the repo

| Where | Name | What |
|---|---|---|
| Fly app secret | `DATABASE_URL` | Set by `fly mpg attach` |
| Fly app secret | OAuth client IDs and secrets, session key | M4 |
| GitHub `staging` env | `FLY_API_TOKEN`, var `PUBLIC_URL` | Deploy token, smoke-test URL |
| GitHub `production` env | `FLY_API_TOKEN`, var `PUBLIC_URL` | M7 |
| GitHub repo secrets | `CLOUDFLARE_API_TOKEN`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | Terraform |
| GitHub repo vars | `CLOUDFLARE_ZONE_ID`, `R2_ENDPOINT`, `STAGING_ENABLED`, `INFRA_ENABLED`, `PRODUCTION_ENABLED` | Terraform and on/off switches |
| Cloudflare | R2 bucket `gaslit-tfstate`, DNSSEC, registrar lock | Terraform state, domain safety |
| Local `.env` | dev OAuth credentials | Never committed |

## Common operations

- **Redeploy main:** Actions > Deploy > Run workflow.
- **Roll back:** run `fly releases -a gaslit-api-staging` to find the previous image, then
  `fly deploy --config "$PWD/deploy/fly/staging.toml" --image <that image>` from the repo root. Afterwards, revert the commit on
  `main` so the next deploy does not bring the bug back.
- **Logs:** `fly logs -a gaslit-api-staging`.
- **Connect to the database:** `fly mpg connect`, or `fly mpg proxy` for a local client. The
  database is never public.
- **Rotate the deploy token:** create a new one (step 2), update the GitHub secret, then revoke
  the old one with `fly tokens list` and `fly tokens revoke <id>`.
~~~~

---

## Part 6: Edits to existing files

Four small edits. The unified diff further down is the exact change.

1. **`.github/dependabot.yml`:** add a weekly `terraform` entry for `/infra/terraform`, so
   the Cloudflare provider gets update PRs like everything else.
2. **`.gitignore`:** ignore `.terraform/`, `*.tfstate`, `*.tfstate.*` and `tfplan`. The
   provider lock file `.terraform.lock.hcl` **is** committed (Saad generates it in runbook
   step 5).
3. **`README.md`:** add `deploy/fly/` and `infra/terraform/` to the layout table, and add a
   short "Deployment" section pointing to `docs/deploy.md`.
4. **`docs/decisions/0001-open-decisions.md`:** change the D1 row to say the name is
   **Gaslit** (`gaslit.dev`), decided 2026-10-05 in ADR 0004, with the placeholders renamed in
   a separate PR.

~~~~diff
diff --git a/.github/dependabot.yml b/.github/dependabot.yml
index c21d4f3..b866e95 100644
--- a/.github/dependabot.yml
+++ b/.github/dependabot.yml
@@ -9,3 +9,6 @@ updates:
   - package-ecosystem: docker
     directory: /server
     schedule: { interval: weekly }
+  - package-ecosystem: terraform
+    directory: /infra/terraform
+    schedule: { interval: weekly }
diff --git a/.gitignore b/.gitignore
index deb186b..b6110de 100644
--- a/.gitignore
+++ b/.gitignore
@@ -10,3 +10,8 @@ build/
 *.egg-info/
 node_modules/
 .DS_Store
+# Terraform (the provider lock file .terraform.lock.hcl IS committed)
+.terraform/
+*.tfstate
+*.tfstate.*
+tfplan
diff --git a/README.md b/README.md
index 4670ad6..c03399e 100644
--- a/README.md
+++ b/README.md
@@ -13,6 +13,8 @@ collected. See [PROJECT_BRIEF.md](PROJECT_BRIEF.md) for the full design.
 | `sdk/python/` | `yourpkg` Python SDK (Apache-2.0, zero runtime dependencies, Python 3.10+) |
 | `server/` | FastAPI service (Python 3.12). Currently `/healthz` and `/readyz` only |
 | `docs/decisions/` | Architecture decision records |
+| `deploy/fly/` | Fly.io app config per environment |
+| `infra/terraform/` | Cloudflare DNS, TLS and firewall (Terraform) |
 | `compose.yaml` | Local Postgres 17 and the server |
 
 ## Development
@@ -25,3 +27,8 @@ make test   # starts compose Postgres (unless DATABASE_URL is set), runs all tes
 make lint   # ruff check, ruff format --check, mypy --strict
 make fmt    # auto-fix lint and formatting
 ```
+
+## Deployment
+
+Every green merge to `main` deploys to staging. Setup, secrets and operations:
+[docs/deploy.md](docs/deploy.md).
diff --git a/docs/decisions/0001-open-decisions.md b/docs/decisions/0001-open-decisions.md
index cf22dec..e17adae 100644
--- a/docs/decisions/0001-open-decisions.md
+++ b/docs/decisions/0001-open-decisions.md
@@ -11,7 +11,7 @@ depend on them, so they are recorded here at M0. The brief's §16 table now link
 
 | # | Decision | Outcome |
 |---|---|---|
-| D1 | Name | Keep placeholders (`yourpkg`, `YOURPKG_KEY`, `rm_live_`, `<domain>`) until PyPI, npm, domain and trademark checks are done |
+| D1 | Name | **Gaslit** (`gaslit.dev`), decided 2026-10-05 in [ADR 0004](0004-deployment.md). Code placeholders are renamed in a separate PR |
 | D2 | Hosting | Fly.io, with Terraform for Cloudflare and the database. AWS stays a later learning extension |
 | D3 | Database | Plain Postgres: native day partitions, our own rollups, queries behind a repository layer |
 | D4 | Frontend | Vite + **Preact** + TypeScript (strict) + **uPlot**, built as a static SPA. FastAPI handles auth. Scaffolded in M4 |
~~~~

---

## Part 7: Commit message and PR description

**Commit message:**

~~~~text
feat(deploy): staging on Fly from day one, Cloudflare via Terraform

- deploy/fly/{staging,production}.toml: Frankfurt, health checks, staging scales to zero
- .github/workflows/deploy.yml: deploy staging after CI passes on main, smoke-test /readyz;
  production job gated by approval and PRODUCTION_ENABLED (M7)
- infra/terraform: proxied DNS to Fly, TLS settings, ingest path allowlist and per-IP rate
  limit; state in R2
- .github/workflows/infra.yml: fmt/validate/plan on PRs, apply on main after approval
- ADR 0004 (also records D1: Gaslit) and docs/deploy.md runbook
~~~~

**PR description:**

~~~~markdown
## What
Deployment skeleton: staging on Fly.io (Frankfurt) is redeployed automatically after CI
passes on `main`. Cloudflare DNS, TLS and firewall are managed by Terraform. Production is
wired up but dormant until M7.

## Why
Bringing staging forward from M7 means every milestone can be tried on a real URL as it
lands. Deploy problems surface now, while the app is just `/healthz`, rather than in launch
week. Full reasoning and trade-offs are in ADR 0004. This PR also records D1: the name is
**Gaslit** (`gaslit.dev`).

## Safe to merge
Nothing deploys or applies until the repo variables `STAGING_ENABLED` and `INFRA_ENABLED`
are set, after the platform setup in `docs/deploy.md`. No SDK or server code changes.

## Not yet verified
Authored without Terraform or flyctl available. See "Known unverified items": provider v5
schema details, R2 `use_lockfile`, Cloudflare token permissions, and the flyctl config path.
The first real runs are the check.

## Next
A separate PR renames the placeholders (`yourpkg` → `gaslit`, `YOURPKG_KEY` → `GASLIT_KEY`,
`rm_live_` → `gl_live_`).
~~~~
