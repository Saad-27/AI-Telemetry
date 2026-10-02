# 0001: Resolve the open decisions (brief §16)

- **Status:** accepted
- **Date:** 2026-10-02

## Context
Brief §16 lists twelve decisions to settle with Saad before building. Later milestones
depend on them, so they are recorded here at M0. The brief's §16 table now links here.

## Decisions

| # | Decision | Outcome |
|---|---|---|
| D1 | Name | Keep placeholders (`yourpkg`, `YOURPKG_KEY`, `rm_live_`, `<domain>`) until PyPI, npm, domain and trademark checks are done |
| D2 | Hosting | Fly.io, with Terraform for Cloudflare and the database. AWS stays a later learning extension |
| D3 | Database | Plain Postgres: native day partitions, our own rollups, queries behind a repository layer |
| D4 | Frontend | Vite + **Preact** + TypeScript (strict) + **uPlot**, built as a static SPA. FastAPI handles auth. Scaffolded in M4 |
| D5 | Licences | SDK Apache-2.0. Server private until launch |
| D6 | Comparison signal | **Official provider status pages only.** The crowd baseline is dropped from scope |
| D7 | Free-tier limits | Per account: **1 project**, 1M calls measured/month, 7-day retention, all read from `entitlements()` |
| D8 | Wire format | Per-minute summaries, every error, first-of-series and a small random sample (§19) |
| D9 | Live feed | Dashboard polls every 3-5 s |
| D10 | Tenant isolation | App-level scoping, IDOR tests **and** Postgres RLS on tenant tables, added as those tables are created |
| D11 | SDK Python floor | 3.10+ (server runs 3.12) |
| D12 | Region | Fly `fra` (Frankfurt), so "EU-hosted" is literally true |

## Rationale for the non-default or delegated choices

**D4: Preact + uPlot.** Saad asked for minimal bloat and decent visuals. Preact (~4 KB gzipped)
has React's component model at a tenth of the size of React + ReactDOM (~45 KB). uPlot (~20 KB
gzipped) is built for dense time series, which is all the dashboard draws. Together they leave
most of the 200 KB JS budget (§21) for app code. Trade-off: a smaller ecosystem than React. If we
ever need a React-only library, `preact/compat` usually covers it.

**D6: official status only.** Saad prefers official provider status as the "is it them?"
signal. This removes §7.1 #12 (you vs everyone), milestone M6, the `share_global` column and
the `global_rollups_*` tables. That makes the product simpler, with no k-anonymity machinery,
and lowers the privacy risk. What we lose: status pages are coarse, often lag and miss partial
slowdowns (e.g. only long prompts slow). Revisit once there are enough users for a crowd
signal to mean something. P6 stays in the brief, parked.

**D7: one project.** Saad wants as many people as possible on the free tier, each with one
project. The `service` tag (§4.1) covers apps with several components.

**D9: polling.** The live feed only re-reads the user's own sampled calls from our database.
It makes no provider calls, so it is not the paid "scheduled checks" feature (§8.1).

**D10: RLS.** It is moderately simple: `ENABLE ROW LEVEL SECURITY` plus a policy comparing
`project_id` with `current_setting('app.project_id')`, which the API sets with `SET LOCAL` in
each request transaction. Ingest and the worker connect with a separate role. It is defence in
depth behind app-level scoping, so a missed `WHERE project_id = ...` cannot leak another
tenant's data. Cost: every dashboard query must run inside a transaction that sets the variable.

## Trade-offs
D6 and D7 narrow the free tier compared with the original brief. Both are easy to reverse:
D7 is a value in `entitlements()`, and D6 can be re-added as an additive milestone.
