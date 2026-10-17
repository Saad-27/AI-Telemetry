# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13 AS uv

FROM python:3.12-slim-bookworm AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /srv
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

FROM python:3.12-slim-bookworm
RUN useradd --system --uid 10001 --no-create-home app
WORKDIR /srv
COPY --from=build /srv /srv
ENV PATH=/srv/.venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# CA bundle for sslmode=verify-full: asyncpg does not fall back to the system store on its own.
ENV PGSSLROOTCERT=/etc/ssl/certs/ca-certificates.crt
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
