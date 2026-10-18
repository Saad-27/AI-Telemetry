#!/usr/bin/env bash
# Local load-test stack with production's limits (ADR 0024), next to (not instead of) the
# compose stack: its own network, Postgres on 127.0.0.1:5433, the app on 127.0.0.1:8001.
#
#   tools/loadtest/run.sh up [burst|baseline]   start Postgres, migrate, create accounts, start the app
#   tools/loadtest/run.sh app [burst|baseline]  restart only the app, with another CPU limit
#   tools/loadtest/run.sh down                  remove everything
#
# Postgres: 0.25 CPU and 1 GB, like Neon at 0.25 CU. The app: the production image in 256 MB,
# with a whole core ("burst", a shared-cpu-1x machine while it has burst balance) or 5 ms of
# CPU per 80 ms ("baseline", the 6.25% it is held to once the balance is spent).
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
NET=gaslit-load PG=gaslit-load-pg APP=gaslit-load-app IMAGE=gaslit-load-server
OWNER_URL=postgresql://app:load@127.0.0.1:5433/app
KEYS=${KEYS:-$ROOT/tools/loadtest/keys.txt}  # git-ignored; test keys for a throwaway database

cpu_limit() {
  case "${1:-burst}" in
    burst) echo "--cpus=1" ;;
    baseline) echo "--cpu-period=80000 --cpu-quota=5000" ;;
    *) echo "unknown CPU mode: $1" >&2; exit 2 ;;
  esac
}

start_app() {
  docker rm -f "$APP" >/dev/null 2>&1 || true
  # shellcheck disable=SC2046
  docker run -d --name "$APP" --network "$NET" $(cpu_limit "${1:-burst}") --memory=256m \
    --memory-swap=256m -e DATABASE_URL="postgresql://app:load@$PG:5432/app" \
    -e METRICS_PORT=9091 -p 127.0.0.1:8001:8000 -p 127.0.0.1:9092:9091 "$IMAGE" >/dev/null
  until curl -sf http://127.0.0.1:8001/readyz >/dev/null; do sleep 0.5; done
  echo "app ready on http://127.0.0.1:8001 (${1:-burst}), metrics on :9092"
}

case "${1:-}" in
  up)
    docker network create "$NET" >/dev/null
    docker run -d --name "$PG" --network "$NET" --cpus=0.25 --memory=1g --memory-swap=1g \
      -e POSTGRES_USER=app -e POSTGRES_PASSWORD=load -e POSTGRES_DB=app \
      -p 127.0.0.1:5433:5432 postgres:17-alpine \
      -c shared_preload_libraries=pg_stat_statements >/dev/null
    until docker exec "$PG" pg_isready -U app -d app -h 127.0.0.1 >/dev/null 2>&1; do sleep 0.5; done
    sleep 1  # the image restarts the server once after initialising
    until docker exec "$PG" pg_isready -U app -d app -h 127.0.0.1 >/dev/null 2>&1; do sleep 0.5; done
    docker exec "$PG" psql -qU app -d app -c "CREATE EXTENSION pg_stat_statements" >/dev/null
    docker build -q -t "$IMAGE" "$ROOT/server" >/dev/null
    (cd "$ROOT/server" && DATABASE_URL=$OWNER_URL uv run alembic upgrade head >/dev/null &&
      DATABASE_URL=$OWNER_URL PYTHONPATH=.:tests uv run python ../tools/loadtest/loadtest.py \
        setup --keys "$KEYS")
    start_app "${2:-burst}"
    ;;
  app) start_app "${2:-burst}" ;;
  down) docker rm -f "$APP" "$PG" >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true ;;
  *) sed -n 2,12p "$0"; exit 2 ;;
esac
