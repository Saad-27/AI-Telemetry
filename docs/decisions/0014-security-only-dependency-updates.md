# 0014: Dependabot opens PRs for security fixes only

- **Status:** accepted
- **Date:** 2026-10-17

## Context
Dependabot opened a PR for every new version of every dependency each week (ADR 0002, brief
§11.8): eight at once in October 2026, none fixing a problem we had. One (Python 3.12 → 3.15
in the server image) could not even build. Saad wants PRs only when an update is needed.

## Options considered
1. Keep weekly version PRs: always current, but constant review work for little value.
2. Group version updates into one PR per ecosystem, monthly: less noise, still routine.
3. Security updates only: a PR only when a published advisory affects a dependency we use.

## Decision
Option 3. Dependabot alerts and security updates are on in the repo settings, and
`.github/dependabot.yml` sets `open-pull-requests-limit: 0`, which stops version-update PRs but
not security-update PRs. Routine upgrades (`uv lock --upgrade`, action SHAs, base image tags,
Python versions) are done by hand at the start of each milestone and tested like any change.
The brief's target of patching critical CVEs within 72 hours stays.

## Trade-offs
- Dependencies drift between milestones, so a later upgrade can be larger. The milestone-start
  refresh keeps the gap to weeks.
- Docker images get no security PRs (no advisories exist for them). The tags float, so a fresh
  build pulls the latest patch release of `python:3.12-slim-bookworm`.
- Revisit if a security PR ever arrives too late, or once users depend on the SDK and a
  stricter update rhythm is expected (M7).
