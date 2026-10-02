# 0002: Repository tooling

- **Status:** accepted
- **Date:** 2026-10-02

## Context
M0 needs a monorepo layout (brief §15), reproducible installs, lint and type checks, a local
Postgres and CI, without adding bloat (§21).

## Options considered
1. **One uv workspace** for all Python packages: one lockfile, but a single shared venv
   forces the SDK and the server onto the same Python version. The SDK must test on 3.10 and
   the server targets 3.12.
2. **One uv project per package** (`sdk/python`, `server`), each with its own lockfile and
   `.python-version`. A root Makefile drives both.
3. Poetry or pip-tools: slower and more moving parts than uv.

## Decision
Option 2.
- **uv** for environments and lockfiles. CI runs `uv sync --locked`.
- **ruff** (lint and format) from a shared root `ruff.toml` that each package extends, so the
  target Python is inferred per package. **mypy --strict** per package.
- **Docker compose** for local Postgres 17 and the server. Ports bind to 127.0.0.1 only.
  `make test` uses the compose database unless `DATABASE_URL` is already set.
- **GitHub Actions** with `contents: read`, actions pinned to commit SHAs and
  `persist-credentials: false`. Dependabot bumps actions, uv locks and base images weekly
  (§11.8). Jobs: SDK on Python 3.10-3.13, server against a Postgres service, Docker build.
- Server image: multi-stage on `python:3.12-slim`, dependencies from the lockfile, non-root user.
- The SDK uses hatchling as a build-time-only backend. Runtime dependencies stay at zero,
  and a test enforces it.

## Trade-offs
Two lockfiles to keep current, which Dependabot handles. Base images are pinned by tag, not
digest. Move to digests in M7 hardening. Alembic, `worker/`, `web/` and pre-commit are
deferred until the milestone that first needs them.
