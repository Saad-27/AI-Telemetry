"""Alembic environment: runs migrations over asyncpg, the driver the app already uses.

Migrations are hand-written SQL (`op.execute`); there are no ORM models to autogenerate from.
"""

import asyncio

import asyncpg
from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import load_settings


def _run(connection: Connection) -> None:
    context.configure(connection=connection, transactional_ddl=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    # Tests pass their own database; otherwise DATABASE_URL, as for the app.
    dsn = context.config.attributes.get("database_url") or load_settings().database_url
    # async_creator hands asyncpg the DSN unchanged, so it is parsed exactly as the app parses
    # it (sslmode and the like), instead of being rewritten into a SQLAlchemy URL.
    engine = create_async_engine(
        "postgresql+asyncpg://", async_creator=lambda: asyncpg.connect(dsn)
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("Offline (--sql) migrations are not supported.")
asyncio.run(_run_online())
