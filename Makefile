SDK    := sdk/python
SERVER := server
PKGS   := $(SDK) $(SERVER)

.PHONY: dev down db test lint fmt

dev:  ## Postgres + server (with reload) on 127.0.0.1:8000
	docker compose up --build

down:
	docker compose down

# Starts the compose Postgres unless DATABASE_URL points at another one.
db:
	@if [ -z "$$DATABASE_URL" ]; then docker compose up -d --wait postgres; fi

test: db
	@for d in $(PKGS); do echo "== test $$d"; (cd $$d && uv run pytest -q) || exit 1; done

lint:
	@for d in $(PKGS); do echo "== lint $$d"; \
		(cd $$d && uv run ruff check . && uv run ruff format --check . && uv run mypy) || exit 1; done

fmt:
	@for d in $(PKGS); do (cd $$d && uv run ruff check --fix . && uv run ruff format .) || exit 1; done
