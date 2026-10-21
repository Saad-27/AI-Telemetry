"""Login roles for the deployed apps (ADR 0026, docs/deploy.md step 2).

Made with SQL, never in Neon's console: console roles join `neon_superuser`, which bypasses
RLS (docs/security-config.md). Each login is a member of one group role from the migrations
and has no rights of its own. Run as the schema owner, on the worker:

    python -m app.db.login_role ingest

prints `DATABASE_URL=<the owner's URL with this login and a new password>` for
`fly secrets import`. Running it again sets a new password, which rotates it.
"""

import asyncio
import secrets
import sys
from urllib.parse import urlsplit, urlunsplit

import asyncpg

from app.core.config import load_settings

# App -> (login role, the group role it joins).
LOGINS = {
    "ingest": ("gaslit_ingest", "ingest_writer"),
    "dashboard": ("gaslit_dashboard", "dashboard_app"),
}


async def set_login(conn: asyncpg.Connection, app: str) -> str:
    """Creates the app's login role, or gives it a new password. Returns the password."""
    login, group = LOGINS[app]
    # 43 URL-safe characters: no quoting needed in SQL or in a URL. DDL takes no parameters,
    # and names come from LOGINS only.
    password = secrets.token_urlsafe(32)
    if await conn.fetchval("SELECT true FROM pg_roles WHERE rolname = $1", login):
        await conn.execute(f"ALTER ROLE {login} LOGIN PASSWORD '{password}'")
    else:
        async with conn.transaction():
            await conn.execute(f"CREATE ROLE {login} LOGIN PASSWORD '{password}'")
            await conn.execute(f"GRANT {group} TO {login}")
    return password


def login_url(owner_url: str, app: str, password: str) -> str:
    """The owner's URL with the app's login: same host, database and TLS settings."""
    parts = urlsplit(owner_url)
    host = parts.netloc.rpartition("@")[2]
    return urlunsplit(parts._replace(netloc=f"{LOGINS[app][0]}:{password}@{host}"))


async def _main(app: str) -> None:
    owner_url = load_settings().database_url
    conn = await asyncpg.connect(owner_url)
    try:
        password = await set_login(conn, app)
    finally:
        await conn.close()
    print(f"DATABASE_URL={login_url(owner_url, app, password)}")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in LOGINS:
        sys.exit(f"usage: python -m app.db.login_role {{{','.join(LOGINS)}}}")
    asyncio.run(_main(sys.argv[1]))
