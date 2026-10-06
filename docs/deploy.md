# Deployment runbook

How Gaslit gets from `main` to a public URL, and every platform setting that lives outside
this repo. Why it works this way: [ADR 0005](decisions/0005-deployment.md).

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
| **Now (during M1)** | Fly configs, deploy and infra workflows, Terraform for Cloudflare, ADR 0005, this runbook | Setup steps 0-6 below: MFA, domain, Fly app and database, Cloudflare, GitHub settings, first Terraform run, certificates | Proves the path to a live URL while there is almost nothing to deploy, so deploy problems never get mixed up with code problems |
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
uses. If `init` rejects `use_lockfile`, remove that line from `versions.tf` (ADR 0005).
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
