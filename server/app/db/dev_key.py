"""A new ingest key for local development: `make dev-key` (or `python -m app.db.dev_key`).

Creates the dev user and project on first use, then a fresh key each run, and prints the key
once. The dashboard does this properly in M4. Refuses to run on Fly.
"""

import asyncio
import os

import asyncpg

from app.core.config import load_settings
from app.ingest.keys import generate

DEV_EMAIL = "dev@localhost"


async def create_dev_key(conn: asyncpg.Connection) -> tuple[str, str]:
    """Returns (project id, key)."""
    async with conn.transaction():
        user = await conn.fetchval("SELECT id FROM users WHERE email = $1", DEV_EMAIL)
        if user is None:
            user = await conn.fetchval(
                "INSERT INTO users (email, display_name) VALUES ($1, 'Dev') RETURNING id",
                DEV_EMAIL,
            )
        project = await conn.fetchval(
            "SELECT id FROM projects WHERE owner_user_id = $1 ORDER BY created_at LIMIT 1", user
        )
        if project is None:
            project = await conn.fetchval(
                "INSERT INTO projects (name, owner_user_id) VALUES ('dev', $1) RETURNING id", user
            )
            await conn.execute(
                "INSERT INTO project_members (project_id, user_id) VALUES ($1, $2)", project, user
            )
        new = generate()
        await conn.execute(
            "INSERT INTO ingest_keys (project_id, key_hash, display_prefix, last4, created_by) "
            "VALUES ($1, $2, $3, $4, $5)",
            project,
            new.key_hash,
            new.display_prefix,
            new.last4,
            user,
        )
    return str(project), new.key


async def _main() -> None:
    if os.environ.get("FLY_APP_NAME"):
        raise SystemExit("dev_key is for local development only.")
    conn = await asyncpg.connect(load_settings().database_url)
    try:
        project, key = await create_dev_key(conn)
    finally:
        await conn.close()
    print(f"Project {project}. New ingest key (shown once):\n\n  export GASLIT_KEY={key}\n")


if __name__ == "__main__":
    asyncio.run(_main())
