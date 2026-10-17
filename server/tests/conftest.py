import asyncio
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

from app.core.config import load_settings

_SERVER = Path(__file__).resolve().parents[1]
_TEST_DB = "gaslit_test"


def _alembic_config(database_url: str) -> Config:
    config = Config(str(_SERVER / "alembic.ini"))
    config.attributes["database_url"] = database_url
    return config


async def _recreate(admin_url: str) -> None:
    conn = await asyncpg.connect(admin_url)
    try:
        await conn.execute(f"DROP DATABASE IF EXISTS {_TEST_DB} WITH (FORCE)")
        await conn.execute(f"CREATE DATABASE {_TEST_DB}")
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def database_url() -> str:
    """A fresh database next to DATABASE_URL's, migrated to head. Leaves the dev data alone."""
    admin_url = load_settings().database_url
    asyncio.run(_recreate(admin_url))
    url = urlunsplit(urlsplit(admin_url)._replace(path=f"/{_TEST_DB}"))
    command.upgrade(_alembic_config(url), "head")
    return url


@pytest.fixture
def alembic_config(database_url: str) -> Config:
    return _alembic_config(database_url)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
