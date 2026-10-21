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
| **M3** | The `worker` app (no public port): migrations as its `release_command`, upkeep every hour, the only holder of the owner's login (ADR 0026). A separate `ingest` app only when load needs it (brief §20), with `ingest` pointed at it in `terraform.tfvars`. Load test, with results in `docs/capacity.md` | Steps 2-6: Neon database, Fly app, Cloudflare tokens, GitHub settings, first Terraform run, certificates | First real database schema and first thing worth hosting. Nothing is paid for before this (ADR 0007) |
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
Fly's Grafana at fly-metrics.net once the apps are deployed (ADR 0021). Alerts are wanted for
no successful worker upkeep in 3 hours (`gaslit_partitions_last_success_timestamp_seconds`,
ADR 0026), 503s, quota flush failures, ingest p95 latency over 200 ms, and dashboard query p95
over 100 ms (ADR 0022's target). Fly's docs say "Fly.io doesn't include built-in alerting on
metrics", so where they run is an open question for Saad (docs/HANDOFF.md).

**Fly** ([ADR 0026](decisions/0026-worker-app-and-login-roles.md)): two apps. The worker
holds Neon's owner connection string and runs migrations and upkeep; the ingest app logs in as
`gaslit_ingest`, which can only write. Install `flyctl`, run `fly auth login`, then from the
repo root:

```bash
# App names are global on Fly. If one is taken, choose another and update
# deploy/fly/*.toml and infra/terraform/terraform.tfvars to match.
fly apps create gaslit-api-production
fly apps create gaslit-worker-production

# 1. The worker gets the owner's connection string from above, and is the only app that does.
#    Typed at a hidden prompt, so it stays out of your shell history and the repo.
read -rsp "Neon owner URL: " DATABASE_URL && fly secrets set DATABASE_URL="$DATABASE_URL" -a gaslit-worker-production; unset DATABASE_URL

# 2. First worker deploy: runs the migrations, then starts the worker.
fly deploy server --config "$PWD/deploy/fly/worker.toml" --ha=false
fly logs -a gaslit-worker-production --no-tail | grep partitions   # "partitions: maintained for ..."

# 3. The ingest app's login, made with SQL on the worker. Its DATABASE_URL goes straight into
#    the ingest app's secrets; the password is never shown. Rerun to rotate it.
fly ssh console -a gaslit-worker-production -C "sh -c 'cd /srv && python -m app.db.login_role ingest'" \
  | tr -d '\r' | grep '^DATABASE_URL=' | fly secrets import -a gaslit-api-production
fly secrets list -a gaslit-api-production   # DATABASE_URL, shown as a digest only

# 4. Deploy tokens, one per app, valid for a year. Put a rotation reminder in your calendar.
fly tokens create deploy -a gaslit-api-production -x 8760h      # GitHub secret FLY_API_TOKEN
fly tokens create deploy -a gaslit-worker-production -x 8760h   # GitHub secret FLY_WORKER_API_TOKEN

# 5. First ingest deploy.
fly deploy server --config "$PWD/deploy/fly/production.toml" --ha=false
curl https://gaslit-api-production.fly.dev/readyz   # {"status":"ok"}; 503 once step 6.4 is done
```

From then on, the deploy workflow does steps 2 and 5 on every merge, worker first. The tokens
go into GitHub in step 4.

The first request after a quiet spell is slow: the Fly machine and the Neon compute both start
from sleep. The deploy smoke test retries for about 30 s to cover this.

### 3. Cloudflare (Terraform access)
1. **State bucket:** R2 > Create bucket `gaslit-tfstate`, with location hint Western Europe.
   The first time, R2 asks you to add the R2 subscription (a checkout that may ask for a
   card). The state file is a few KB, far inside the free 10 GB a month, so it costs $0.
2. **State credentials:** R2 > Manage API tokens > Create: **Object Read & Write**, limited to
   the `gaslit-tfstate` bucket. This gives an Access Key ID and Secret Access Key. The
   endpoint is `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`.
3. **Terraform's API token:** Manage account > Account API tokens > Create Token. An account
   token, not a user one: Cloudflare recommends them for CI, and every product used here
   supports them. Give it these permissions and an expiry about a year out:
   - Zone > DNS > Edit
   - Zone > Zone Settings > Edit
   - Zone > Zone WAF > Edit
   - Zone > Transform Rules > Edit (the edge secret header, step 3.5)
   - Account > Account Rulesets > Read (Cloudflare lists it as needed for transform rules)

   Zone resources: Include > Specific zone > `gaslit.dev`. If an apply fails with a permission
   error on a ruleset, add the permission it names.
4. If the zone already has custom firewall, rate-limiting or request header transform rules
   made in the dashboard, delete them. Terraform owns those phases and the first apply would
   fail.
5. **Edge secret** ([ADR 0027](decisions/0027-edge-secret-header.md)): Cloudflare adds it to
   every request as the `X-Gaslit-Edge` header, and the app refuses requests without it, so
   nobody can skip Cloudflare by calling `*.fly.dev`. Make one with `openssl rand -hex 32`. It
   goes into GitHub (step 4), your shell for step 5, and Fly at step 6. Store it nowhere else.

### 4. GitHub repository settings

**Environments** (Settings > Environments):

| Environment | Protection | Secrets | Variables |
|---|---|---|---|
| `production` | Required reviewer: you. Deployment branches: `main` only | `FLY_API_TOKEN`, `FLY_WORKER_API_TOKEN` (from step 2) | `PUBLIC_URL` = `https://gaslit-api-production.fly.dev` |
| `infra` | Required reviewer: you. Branches: `main`. Create it **before** setting `INFRA_ENABLED`: a job naming a missing environment makes GitHub create an empty one, with no reviewer | none | none |

Required reviewers on environments are free only on public repositories. If the repo goes
private, they need GitHub Enterprise, and both approval gates would stop working.

**Repository secrets** (Settings > Secrets and variables > Actions > Secrets):
`CLOUDFLARE_API_TOKEN`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `EDGE_SECRET` (from step 3).

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
read -rsp "Edge secret: " TF_VAR_edge_secret && export TF_VAR_edge_secret   # step 3.5
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
fly certs setup app.gaslit.dev    -a gaslit-api-production   # prints the two records below
fly certs setup ingest.gaslit.dev -a gaslit-api-production
```

Single-hostname certificates cost at most $0.10 a month each on Fly (some plans include a few
free). Ignore the "route traffic" options `setup` prints: Terraform already points each name
at the app through Cloudflare. Under "Additional DNS records", each hostname has:

- **Ownership TXT record**, `_fly-ownership.<host>`: proves the domain is ours, since its DNS
  answers with Cloudflare's addresses rather than Fly's.
- **ACME DNS Challenge**, a CNAME from `_acme-challenge.<host>` to a `flydns.net` name: lets Let's
  Encrypt check the domain through DNS (DNS-01). Through the proxy it cannot reach Fly
  directly, so this is how the certificate is issued and renewed every few months.

1. Put both values for each host into `infra/terraform/terraform.tfvars` (`fly_ownership` and
   `acme_challenge`), then open a PR, merge it and approve the apply. Terraform makes both
   records DNS only (grey cloud), as they must be.
2. Run `fly certs check app.gaslit.dev -a gaslit-api-production` until the certificate is issued.
3. Change the `production` environment's `PUBLIC_URL` to `https://app.gaslit.dev`, and check
   `curl https://app.gaslit.dev/readyz` returns `{"status":"ok"}`.
4. Close the `*.fly.dev` bypass. The app restarts and from then on answers 503 to anything
   without Cloudflare's header, except `/healthz` (Fly's own checks):

   ```bash
   read -rsp "Edge secret: " EDGE_SECRET && fly secrets set EDGE_SECRET="$EDGE_SECRET" -a gaslit-api-production; unset EDGE_SECRET
   curl https://app.gaslit.dev/readyz                     # {"status":"ok"}
   curl https://gaslit-api-production.fly.dev/readyz      # {"error":"edge_check_failed"}
   ```

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
| Fly secret, worker app | `DATABASE_URL` | Neon's owner connection string (direct), `sslmode=verify-full`, without `channel_binding` (step 2) |
| Fly secret, ingest app | `DATABASE_URL` | The same with the `gaslit_ingest` login, made by `app.db.login_role` (step 2) |
| Fly secret, ingest app | `EDGE_SECRET` | The header value Cloudflare adds; requests without it get 503 (step 6) |
| Fly secret, ingest app | OAuth client IDs and secrets, session key | M4 |
| GitHub `production` env | `FLY_API_TOKEN`, `FLY_WORKER_API_TOKEN`, var `PUBLIC_URL` | Deploy tokens (one per app), smoke-test URL |
| GitHub repo secrets | `CLOUDFLARE_API_TOKEN`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `EDGE_SECRET` | Terraform |
| GitHub repo vars | `CLOUDFLARE_ZONE_ID`, `R2_ENDPOINT`, `PRODUCTION_ENABLED`, `INFRA_ENABLED` | Terraform and on/off switches |
| Cloudflare | R2 bucket `gaslit-tfstate`, DNSSEC, registrar lock | Terraform state, domain safety |
| Local `.env` | dev OAuth credentials | Never committed |

## Common operations

- **Redeploy main:** Actions > Deploy > Run workflow.
- **Roll back:** run `fly releases -a gaslit-api-production --image` to find the previous image, then
  `fly deploy --config "$PWD/deploy/fly/production.toml" --image <that image>` from the repo root. Afterwards, revert the commit on
  `main` so the next deploy does not bring the bug back.
- **Logs:** `fly logs -a gaslit-api-production`, and `-a gaslit-worker-production` for
  migrations and upkeep.
- **Connect to the database:** the SQL editor in Neon's dashboard, or `psql` with the
  connection string. Unlike Fly's private network, Neon is reachable from the internet, protected
  by its password and TLS. Check Neon's IP allow-list options before M7 (ADR 0007).
- **Rotate a deploy token:** create a new one (step 2), update the GitHub secret, then revoke
  the old one with `fly tokens list -a <app>` and `fly tokens revoke <id>`.
- **Rotate the edge secret** (if it ever appears anywhere), without refusing real traffic:
  1. Make a new one (`openssl rand -hex 32`). Set the app to accept both, typed at hidden
     prompts: `read -rsp "New: " NEW && read -rsp "Old: " OLD && fly secrets set
     EDGE_SECRET="$NEW $OLD" -a gaslit-api-production`.
  2. Put the new one in the GitHub secret `EDGE_SECRET`, then Actions > Infra > Run workflow
     and approve the apply. Cloudflare now sends the new value.
  3. `fly secrets set EDGE_SECRET="$NEW" -a gaslit-api-production` to drop the old one, then
     `unset NEW OLD`.
- **Rotate the ingest login's password:** rerun step 2.3. The ingest app restarts with the new
  secret. The owner's password: reset it in Neon, then repeat step 2.1 with the new URL.
