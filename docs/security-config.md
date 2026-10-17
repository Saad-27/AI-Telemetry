# Security configuration requirements

Settings that must be right so users' data, keys and apps stay safe. Most of these are
configuration, not code, so tests cannot catch them; check this list at each milestone and
before launch. The brief's §11.11 checklist is the launch gate; this file covers how to meet it.
How to apply a setting is in [deploy.md](deploy.md).

**Status:** **Done** (in place and verified) · **Step N** (M3 step that adds it) · **M4/M7**
(that milestone) · **Gap** (an open problem, with the fix proposed).

## 1. Database connection (Neon)

| Requirement | Why (what leaks otherwise) | Status |
|---|---|---|
| `DATABASE_URL` uses `sslmode=verify-full` | asyncpg's `sslmode=require` encrypts but accepts **any** certificate, so anyone on the network path between Fly and Neon could read the password and every query (checked in asyncpg 0.31 source). The image sets `PGSSLROOTCERT` to the system CA bundle; Neon's certificates come from Let's Encrypt | **Done:** the server refuses to start on Fly without it (`app/core/config.py`) |
| No `channel_binding` in the URL | Not a leak: Postgres rejects it and the app cannot connect | Runbook step 2 |
| The URL exists only as a Fly secret | Typed with `read -s`, never in shell history, the repo or logs | Runbook step 2 |
| Login roles for the app are created **with SQL**, never in the Neon console | Console roles join `neon_superuser`, which has `BYPASSRLS`, so RLS would silently stop protecting tenants ([Neon](https://neon.com/docs/manage/roles)) | Step 8 |
| Ingest connects as a member of `ingest_writer`, the dashboard as a member of `dashboard_app`. Only migrations and partition jobs use the owner | The owner bypasses RLS and can read every tenant's data. A bug in ingest must not be able to read anything | Step 8 and M4. **Today the app connects as the owner** |
| Postgres statement logging stays at Neon's default (no `log_statement = all`, no parameter logging) | Logged parameters would put customer metrics and key hashes in Neon's logs | Check at step 8 |
| Strong generated password, rotated if it ever appears anywhere | Neon's IP allowlist is a paid-plan feature, so the endpoint is reachable from the internet; TLS plus the password are the protection | Step 8 |

## 2. Tenant isolation in the database

| Requirement | Why | Status |
|---|---|---|
| RLS on `summary_records` and `call_samples`; dashboard queries run in a transaction with `SET LOCAL app.project_id` | A missed `WHERE project_id = ...` cannot return another tenant's rows | **Done** in the schema (ADR 0013); takes effect with the roles above |
| Grants only on the partitioned parents, never on partitions | RLS applies through the parent only. Direct access to a partition would skip it | **Done** |
| `ingest_writer` cannot read data, users or key prefixes | A leaked or buggy ingest process cannot be used to read tenants' data | **Done**, tested |
| Every dashboard route goes through `require_project_member`; unknown and foreign projects both return 404; IDOR suite generated from the route table | Stops one user reading another's project by guessing IDs | M4 |

## 3. Hosting (Fly)

| Requirement | Why | Status |
|---|---|---|
| **Requests that skip Cloudflare are rejected** | `gaslit-api-production.fly.dev` reaches the app directly, around Cloudflare's firewall, ingest path filter and rate limit. Proposed fix: a Cloudflare request-header rule adds a secret header, the app rejects requests without it, and the deploy smoke test goes through `PUBLIC_URL` | **Gap**: decide at step 8 |
| The app reads the client IP only from a header it can trust (`CF-Connecting-IP`, once the gap above is closed) | Otherwise per-IP limits on invalid keys can be dodged by forging the header | Steps 3-4 |
| Secrets only through `fly secrets set`, never in `production.toml` | The repo is public | **Done** (the file has none) |
| Non-root container, no dev dependencies in the image | Limits damage from a compromised process | **Done** (`uid 10001`, `uv sync --no-dev`) |
| Logs may hold client IPs (personal data, GDPR); keep retention short and list Fly, Neon and Cloudflare as sub-processors | Privacy policy accuracy | M7 |

## 4. Edge (Cloudflare, Terraform)

| Requirement | Why | Status |
|---|---|---|
| SSL mode **Full (strict)**, TLS 1.2 minimum, Always HTTPS, HSTS | No plaintext hop between users and Fly | **Done** in Terraform, applied at runbook step 5 |
| Ingest hosts accept only `/v1/ingest`, `/healthz`, `/readyz` and ACME | Scanners never reach the app | **Done** in Terraform |
| Per-IP rate limit on ingest | Flood protection before the app | **Done** in Terraform |
| Registrar lock on, DNSSEC active | Stops domain hijacking, which would redirect users' SDK traffic and keys to an attacker | Confirm both: DNSSEC was "pending" on 2026-10-14 |

## 5. Ingest app behaviour (M3 steps 2-5)

| Requirement | Why | Status |
|---|---|---|
| Never log request bodies, keys, `Authorization` headers or driver error text. Log the exception type only, as `/readyz` does | Postgres errors can quote row values (for example a duplicate key hash) | Step 3, with a test on captured log output (§11.11) |
| No CORS headers on ingest | Keys belong on servers, never in browser code | Step 3 |
| Size caps before reading the body; gunzip streamed with a 5 MB cap | Stops memory exhaustion and zip bombs | Step 3 |
| Pydantic with `extra="forbid"`, strict enums and regexes | Content-like or oversized strings can never be stored (P1), and stored XSS is blocked at the door | Step 3 |
| Error responses carry a reason code, never input or a stack trace | Responses must not echo what was sent | Step 3 |
| Only the SHA-256 of a key is stored; key cache at most 60 s | A database leak does not leak usable keys; revocation takes effect within a minute | Step 2 |
| Parameterised SQL only (identifiers built from constants, as in `app.db.partitions`) | SQL injection | Every step |

## 6. Dashboard (M4)

Session cookie `__Host-session` (`Secure`, `HttpOnly`, `SameSite=Lax`), CORS only for
`https://app.gaslit.dev`, the CSP and headers in brief §11.2, OAuth with `state` and PKCE,
identity linking by provider subject (never by email), keys shown once. Fill this section in
as M4 is built.

## 7. SDK defaults (inside users' apps)

| Requirement | Why | Status |
|---|---|---|
| HTTPS only, except plain HTTP to loopback; TLS verification cannot be turned off | Users' keys and metrics never cross the network in clear text | **Done** (`_config.py`, `_sender.py`) |
| Keys must start with `gl_`; malformed keys are never sent | A provider key pasted into `GASLIT_KEY` never leaves the user's app | **Done** |
| Warnings never echo values; `debug` prints payloads to the user's own console only | Keys and endpoints stay out of logs | **Done** |
| `DEFAULT_ENDPOINT` changes from `ingest.gaslit.invalid` to `https://ingest.gaslit.dev` | Without it, SDKs installed with defaults send nothing | Before 0.1.0 (M7) |
| Privacy sentinel and fail-open tests on every PR | P1-P4 | **Done** in CI |

## 8. Repository, CI and accounts

| Requirement | Why | Status |
|---|---|---|
| `.env` and `.env.*` git-ignored | Local credentials never committed | **Done** |
| No secrets in git history | The repo is public | **Done**: history scanned for key, token and connection-string patterns on 2026-10-17, none found |
| Actions pinned to SHAs, `contents: read`, `persist-credentials: false`, no `pull_request_target`; deploy and infra jobs refuse fork events | Stops a pull request from a fork reaching secrets | **Done** |
| `production` and `infra` environments need Saad's approval | Nothing deploys or changes DNS unseen | Runbook step 4 |
| GitHub secret scanning and push protection | Blocks a pushed secret. Free on public repos | Off (Saad's choice) |
| MFA on GitHub, PyPI, Cloudflare, Fly, Neon and email | Account takeover is the shortest path to users' data | Runbook step 0; confirm for Fly and Neon when created |
| PyPI Trusted Publishing, no stored upload token | A stolen token could ship malware into users' apps | M7 (the placeholder token was deleted) |
