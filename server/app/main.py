import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.config import Settings, load_settings
from app.db import DB_TIMEOUT_S
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
        app.state.keys = KeyStore(app.state.pool)
        app.state.sink = PostgresSink(app.state.pool)
        app.state.rate = RateLimiter()
        quota = app.state.quota = Quota(app.state.pool)
        flusher = asyncio.create_task(quota.flush_every())
        try:
            yield
        finally:
            flusher.cancel()
            with suppress(asyncio.CancelledError):
                await flusher
            await quota.flush_logged()  # Fly stops idle machines; keep their counts
            await app.state.pool.close()

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
