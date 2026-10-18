import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_client import start_http_server

from app.core.config import Settings, load_settings
from app.core.entitlements import longest_retention_days
from app.db import DB_TIMEOUT_S
from app.db.partitions import maintain_daily
from app.db.totals import Totals
from app.ingest.keys import KeyStore
from app.ingest.limits import Quota, RateLimiter
from app.ingest.routes import router as ingest_router
from app.ingest.sink import PostgresSink

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    # Uvicorn configures only its own loggers; this shows the app's INFO lines too.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # min_size=0: no connection at startup, so liveness works while the DB is down.
        app.state.pool = await asyncpg.create_pool(
            settings.database_url, min_size=0, max_size=5, timeout=DB_TIMEOUT_S
        )
        quota = app.state.quota = Quota(app.state.pool)
        # Accounts over quota are answered from memory, so their SDKs' checks every minute let
        # the database sleep (ADR 0019).
        app.state.keys = KeyStore(app.state.pool, keep=quota.blocks, learn=quota.learn)
        app.state.sink = PostgresSink(app.state.pool)
        app.state.rate = RateLimiter()
        # The dashboard's totals, built while this machine receives summaries (ADR 0023).
        totals = app.state.totals = Totals(app.state.pool)
        flusher = asyncio.create_task(quota.flush_every())
        builder = asyncio.create_task(totals.build_every())
        # Partitions and retention, at startup and then once a day (ADR 0020). In the
        # background, so the app starts even while the database is down.
        upkeep = asyncio.create_task(maintain_daily(app.state.pool, longest_retention_days()))
        # Fly scrapes this port over its private network; it is not in [http_service], so it
        # is never public. A thread, so a scrape never waits for the event loop.
        port = settings.metrics_port
        metrics = app.state.metrics = start_http_server(port) if port is not None else None
        try:
            yield
        finally:
            for task in (flusher, builder, upkeep):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await quota.flush_logged()  # Fly stops idle machines; keep their counts
            await app.state.pool.close()
            if metrics is not None:
                server, thread = metrics
                server.shutdown()
                server.server_close()
                thread.join()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(ingest_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        try:
            async with request.app.state.pool.acquire(timeout=DB_TIMEOUT_S) as conn:
                await conn.fetchval("SELECT 1", timeout=DB_TIMEOUT_S)
        except Exception as exc:
            # Type only: driver messages can echo connection details.
            log.warning("readyz: database unreachable (%s)", type(exc).__name__)
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    return app


app = create_app()
