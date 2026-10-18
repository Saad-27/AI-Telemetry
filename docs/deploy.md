# Deployment runbook

How Gaslit gets from `main` to a public URL, and every platform setting that lives outside
this repo. Why it works this way: [ADR 0005](decisions/0005-deployment.md) and
[ADR 0006](decisions/0006-production-only.md).

**Rule:** config lives in the repo; secrets and account settings live on the platforms. This
file lists every one of them by name, so the whole setup can be rebuilt from scratch.

## How a change goes live

```
PR ──► CI (lint, tests, docker build) ──► merge to main
                                              │
                         CI passes on main ───┤
                                              ▼
              you approve in GitHub ──► flyctl deploy ──► production ──► smoke test /readyz
```

There is no staging ([ADR 0006](decisions/0006-production-only.md)). Changes under
`infra/terraform/` take a separate path: the PR shows a Terraform plan, and merging applies it
after you approve.

| Environment | Fly app | URL | Deploys |
|---|---|---|---|
| local | none | `http://127.0.0.1:8000` | `make dev` |
| production | `gaslit-api-production` | `https://app.gaslit.dev` (until DNS is ready: `https://gaslit-api-production.fly.dev`) | every green merge to `main`, after you approve it |

## Rollout by milestone

Deployment grows with the code, so each piece arrives when the milestone that needs it lands.

| When | What gets built (code) | What you do (platforms) | Why then |
|---|---|---|---|
| **Done** | Fly config, deploy and infra workflows, Terraform for Cloudflare, ADRs 0005-0007, this runbook, placeholder rename | Steps 0, 1 and 8 below: MFA, domain, PyPI name | Free or near-free, and the names could be taken |
| **M2** | Nothing new for deployment | Nothing | SDK work runs in users' apps, not on our servers |
| **M3** | Alembic `release_command` turned on in `production.toml`. Separate Fly apps for `ingest` and `worker` (the worker has no public port) only when load needs it (brief §20), with `ingest` pointed at the ingest app in `terraform.tfvars`. Load test, with results in `docs/capacity.md` | Steps 2-6: Neon database, Fly app, Cloudflare tokens, GitHub settings, first Terraform run, certificates | First real database schema and first thing worth hosting. Nothing is paid for before this (ADR 0007) |
| **M4** | Web SPA build in the deploy, OAuth and session-key settings, security headers | Step 7 (OAuth apps), then `fly secrets set` for the client IDs, secrets and session key | Login and the dashboard need them. Production becomes a clickable website |
| **M5** | Status poller inside the worker | Nothing | Outbound requests only, so no infrastructure change |
| **M7** | `release.yml` for PyPI Trusted Publishing, base images pinned by digest, uptime check, Grafana | A tested backup restore, step 8. Decide whether alpha needs a staging app (ADR 0006) | Launch: the security checklist in brief §11.11 must be fully ticked |

## When you can test

| From | What | How |
|---|---|---|
| Now | SDK unit tests | `make test` |
| End of M1 | Exactly what the SDK sends | `gaslit.init(debug=True)` prints every payload locally, no account needed |
| End of M2 | Real OpenAI and Anthropic calls | Your own scripts with debug output, or the fake provider (no token cost) |
| End of M3 | Full pipeline: app → ingest → database → rollups | Locally with `make dev`; hosted once steps 2-6 are done |
| End of M4 | The real website: sign up, create a key, see calls on the dashboard | `https://app.gaslit.dev` |
| M7 | Real users (private alpha) | `https://app.gaslit.dev` |

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

### 2. Database (Neon) and Fly.io
**Database** ([ADR 0007](decisions/0007-neon-and-scale-to-zero.md)): create a Neon account and a
project named `gaslit` on the Free plan, region **AWS Europe Central 1 (Frankfurt)**, Postgres 17.
Copy the **direct** connection string (not the pooled one; the app keeps its own small pool).

Edit its query string before storing it:

- Remove `&channel_binding=require`. asyncpg passes query parameters it does not know to
  Postgres as settings, and Postgres rejects `channel_binding`.
- Change `sslmode=require` to `sslmode=verify-full`. With `require`, asyncpg encrypts but accepts
  any certificate, so anyone on the network path could read the password. `verify-full` checks
  Neon's certificate against the CA bundle in the image (`PGSSLROOTCERT`). The server refuses to
  start on Fly without it ([security-config.md](security-config.md)).

**Fix the compute size, which caps the bill.** In the branch's compute settings, set autoscaling
to a minimum and maximum of **0.25 CU**, and leave scale-to-zero on. On the paid plan, compute
then costs at most about $19.35 a month ($0.106 per CU-hour × 730 hours), however much traffic
arrives. Past its capacity, ingest slows and returns 503, and SDKs retry or drop. Storage, the
other cost, is bounded by `entitlements()` ([ADR 0017](decisions/0017-rate-limit-and-quota.md)).
Raise the size only when the load test or production metrics show it is needed. Metrics are in
Fly's Grafana at fly-metrics.net once the app is deployed (ADR 0021): set alerts there for no
successful partition run in 2 days, 503s, quota flush failures, ingest p95 latency over 200 ms
dashboard query p95 over 100 ms (ADR 0022's target), and any failed totals build (ADR 0023).

**Fly:** install `flyctl` and run `fly auth login`, then:

```bash
# App names are global on Fly. If one is taken, choose another and update
# deploy/fly/production.toml and infra/terraform/terraform.tfvars to match.
fly apps create gaslit-api-production

# Stores the Neon connection string as the app secret DATABASE_URL. Nothing goes in the repo.
# Run it with no value on the command line, so the URL stays out of your shell history:
read -rsp "Neon URL: " DATABASE_URL && fly secrets set DATABASE_URL="$DATABASE_URL" -a gaslit-api-production --stage; unset DATABASE_URL

# Deploy token scoped to this one app, valid for a year. Put a rotation reminder in your calendar.
fly tokens create deploy -a gaslit-api-production -x 8760h
```

The token is a GitHub secret (step 4). Optional first manual deploy, from the repo root:

```bash
fly deploy server --config "$PWD/deploy/fly/production.toml" --ha=false
curl https://gaslit-api-production.fly.dev/readyz   # {"status":"ok"}
```

The first request after a quiet spell is slow: the Fly machine and the Neon compute both start
from sleep. The deploy smoke test retries for about 30 s to cover this.

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
| `production` | Required reviewer: you. Deployment branches: `main` only | `FLY_API_TOKEN` (from step 2) | `PUBLIC_URL` = `https://gaslit-api-production.fly.dev` |
| `infra` | Required reviewer: you. Branches: `main` | none | none |

Required reviewers on environments are free only on public repositories. If the repo goes
private, they need GitHub Enterprise, and both approval gates would stop working.

**Repository secrets** (Settings > Secrets and variables > Actions > Secrets):
`CLOUDFLARE_API_TOKEN`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` (from step 3).

**Repository variables** (same page > Variables):

| Variable | Value | Effect |
|---|---|---|
| `CLOUDFLARE_ZONE_ID` | from step 1 | Terraform's `zone_id` |
| `R2_ENDPOINT` | `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` | Terraform state location |
| `PRODUCTION_ENABLED` | `true` once steps 2 and 4 are done | turns on deploys |
| `INFRA_ENABLED` | `true` once step 5 is done | turns on Terraform plan and apply |

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
uses. If `init` rejects `use_lockfile`, remove that line from `versions.tf` (ADR 0005).
Then set `INFRA_ENABLED=true`. From now on, merges apply the changes after you approve them.

### 6. Custom domain

```bash
fly certs add app.gaslit.dev    -a gaslit-api-production
fly certs add ingest.gaslit.dev -a gaslit-api-production
fly certs setup app.gaslit.dev    -a gaslit-api-production   # prints the _fly-ownership value
fly certs setup ingest.gaslit.dev -a gaslit-api-production
```

1. Put each `_fly-ownership` value into `infra/terraform/terraform.tfvars`, then open a PR, merge it and approve the apply.
2. Run `fly certs check app.gaslit.dev -a gaslit-api-production` until the certificate is issued.
3. Change the `production` environment's `PUBLIC_URL` to `https://app.gaslit.dev`.

### 7. OAuth apps (needed in M4; can be done now)
The callback path is `/auth/{provider}/callback` (brief §9.3). The secret names are fixed in M4.

- **GitHub:** Settings > Developer settings > OAuth Apps. A GitHub OAuth app allows only one
  callback URL, so create two:
  - `Gaslit (dev)`, with callback `http://localhost:8000/auth/github/callback`
  - `Gaslit`, with callback `https://app.gaslit.dev/auth/github/callback`
- **Google:** Cloud Console > APIs & Services.
  1. Set up the OAuth consent screen: External, scopes `openid email profile`.
  2. Go to Credentials > OAuth client ID > Web application, and add both callback URLs above
     with `google` in place of `github`.
- **Where the secrets go:** dev secrets in your local `.env`, which is git-ignored. Production
  secrets via `fly secrets set ... -a gaslit-api-production`.

### 8. PyPI and npm names
**Done 2026-10-14:** a placeholder `gaslit` 0.0.0 holds the name on PyPI. It was uploaded once
with a temporary account-wide token, which was then deleted.

In M7, when `release.yml` exists: on PyPI, go to the project's **Manage > Publishing** and add a
trusted publisher (owner `Saad-27`, repo `AI-Telemetry`, workflow `release.yml`, environment
`pypi`). Releases then need no token. The first real version is `0.1.0`. Do the same on npm when
the TypeScript SDK starts.

## Inventory: everything that lives outside the repo

| Where | Name | What |
|---|---|---|
| Fly app secret | `DATABASE_URL` | Neon direct connection string, `sslmode=verify-full`, without `channel_binding` (step 2) |
| Fly app secret | OAuth client IDs and secrets, session key | M4 |
| GitHub `production` env | `FLY_API_TOKEN`, var `PUBLIC_URL` | Deploy token, smoke-test URL |
| GitHub repo secrets | `CLOUDFLARE_API_TOKEN`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | Terraform |
| GitHub repo vars | `CLOUDFLARE_ZONE_ID`, `R2_ENDPOINT`, `PRODUCTION_ENABLED`, `INFRA_ENABLED` | Terraform and on/off switches |
| Cloudflare | R2 bucket `gaslit-tfstate`, DNSSEC, registrar lock | Terraform state, domain safety |
| Local `.env` | dev OAuth credentials | Never committed |

## Common operations

- **Redeploy main:** Actions > Deploy > Run workflow.
- **Roll back:** run `fly releases -a gaslit-api-production --image` to find the previous image, then
  `fly deploy --config "$PWD/deploy/fly/production.toml" --image <that image>` from the repo root. Afterwards, revert the commit on
  `main` so the next deploy does not bring the bug back.
- **Logs:** `fly logs -a gaslit-api-production`.
- **Connect to the database:** the SQL editor in Neon's dashboard, or `psql` with the
  connection string. Unlike Fly's private network, Neon is reachable from the internet, protected
  by its password and TLS. Check Neon's IP allow-list options before M7 (ADR 0007).
- **Rotate the deploy token:** create a new one (step 2), update the GitHub secret, then revoke
  the old one with `fly tokens list` and `fly tokens revoke <id>`.
