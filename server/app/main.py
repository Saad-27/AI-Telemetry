import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.config import Settings, load_settings

log = logging.getLogger(__name__)

_DB_TIMEOUT_S = 2.0


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # min_size=0: no connection at startup, so liveness works while the DB is down.
        app.state.pool = await asyncpg.create_pool(
            settings.database_url, min_size=0, max_size=5, timeout=_DB_TIMEOUT_S
        )
        try:
            yield
        finally:
            await app.state.pool.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        try:
            async with request.app.state.pool.acquire(timeout=_DB_TIMEOUT_S) as conn:
                await conn.fetchval("SELECT 1", timeout=_DB_TIMEOUT_S)
        except Exception as exc:
            # Type only: driver messages can echo connection details.
            log.warning("readyz: database unreachable (%s)", type(exc).__name__)
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    return app


app = create_app()
