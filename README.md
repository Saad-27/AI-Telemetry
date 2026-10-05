# <Product>

Open-source SDK plus a hosted dashboard that answers "is it me, or the model provider?"
using metadata from your app's own AI model calls. No prompts, outputs or keys are ever
collected. See [PROJECT_BRIEF.md](PROJECT_BRIEF.md) for the full design.

**Status:** M0 (foundations). Nothing is instrumented yet.

## Layout

| Path | What |
|---|---|
| `sdk/python/` | `yourpkg` Python SDK (Apache-2.0, zero runtime dependencies, Python 3.10+) |
| `server/` | FastAPI service (Python 3.12). Currently `/healthz` and `/readyz` only |
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

Every green merge to `main` deploys to staging. Setup, secrets and operations:
[docs/deploy.md](docs/deploy.md).
