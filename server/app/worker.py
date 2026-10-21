"""The worker (brief §9.8): partition upkeep, the dashboard's totals and retention, at startup
and then every hour (ADR 0026). It runs on its own Fly app, the only one holding the schema
owner's connection string, so the ingest app's login can only write (ADR 0013). Fly scrapes
its metrics as it does the app's. `python -m app.worker`
"""

import asyncio
import logging
from contextlib import suppress

from prometheus_client import start_http_server

from app.core.config import Settings, load_settings
from app.core.entitlements import longest_retention_days
from app.db.partitions import maintain_every


async def run(settings: Settings) -> None:
    if settings.metrics_port is not None:
        start_http_server(settings.metrics_port)
    await maintain_every(settings.database_url, longest_retention_days())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    with suppress(KeyboardInterrupt):  # Fly stops machines with SIGINT
        asyncio.run(run(load_settings()))
