# Gaslit

Open-source SDK plus a hosted dashboard that answers "is it me, or the model provider?"
using metadata from your app's own AI model calls. No prompts, outputs or keys are ever
collected. See [PROJECT_BRIEF.md](PROJECT_BRIEF.md) for the full design.

**Status:** M3 (ingest and storage) done; M4 (dashboard) next. The Python SDK measures
`openai` and `anthropic` calls automatically and anything else through `track()`. Ingest is
hosted at `https://ingest.gaslit.dev`.

**Capacity** on the smallest production setup (one Fly `shared-cpu-1x` machine, Neon 0.25 CU),
2026-10-21: about 25-30 requests/s (55-65 records/s) indefinitely, and at least 300
requests/s (675 records/s) at p95 45 ms while the machine's CPU burst balance lasts; the limit
was not reached. Method and details: [docs/capacity.md](docs/capacity.md).

## Layout

| Path | What |
|---|---|
| `sdk/python/` | `gaslit` Python SDK (Apache-2.0, zero runtime dependencies, Python 3.10+) |
| `server/` | FastAPI service (Python 3.12). Currently `/healthz` and `/readyz` only |
| `tools/fake-provider/` | Fake OpenAI and Anthropic API for tests and demos (no tokens spent) |
| `examples/` | Sample apps that print what the SDK sends, offline |
| `docs/decisions/` | Architecture decision records |
| `deploy/fly/` | Fly.io app config per environment |
| `infra/terraform/` | Cloudflare DNS, TLS and firewall (Terraform) |
| `compose.yaml` | Local Postgres 17 and the server |

## Development

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```bash
make dev    # Postgres + server on http://127.0.0.1:8000 (reloads on change)
make test   # starts compose Postgres (unless DATABASE_URL is set), runs all tests
make lint   # ruff check, ruff format --check, mypy --strict
make fmt    # auto-fix lint and formatting
```

## Deployment

Every green merge to `main` deploys to production after you approve it. Setup, secrets and
operations: [docs/deploy.md](docs/deploy.md).
