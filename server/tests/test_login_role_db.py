"""Login roles for the deployed apps, against Postgres (ADR 0026)."""

import asyncpg
import pytest

from app.db.login_role import login_url, set_login

NEON = "postgresql://neondb_owner:secret@ep-x-1.eu-central-1.aws.neon.tech/neondb"


def test_the_url_keeps_host_database_and_tls() -> None:
    assert login_url(f"{NEON}?sslmode=verify-full", "ingest", "pw") == (
        "postgresql://gaslit_ingest:pw@ep-x-1.eu-central-1.aws.neon.tech/neondb?sslmode=verify-full"
    )
    assert login_url("postgresql://127.0.0.1:5432/app", "dashboard", "pw") == (
        "postgresql://gaslit_dashboard:pw@127.0.0.1:5432/app"
    )


@pytest.mark.db
@pytest.mark.anyio
async def test_the_ingest_login_has_only_ingest_writers_rights(database_url: str) -> None:
    owner = await asyncpg.connect(database_url)
    try:
        first = await set_login(owner, "ingest")
        password = await set_login(owner, "ingest")  # again: a new password
        flags = await owner.fetchrow(
            "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb FROM pg_roles "
            "WHERE rolname = 'gaslit_ingest'"
        )
    finally:
        await owner.close()
    assert first != password and len(password) == 43
    assert flags is not None and not any(flags.values())

    login = await asyncpg.connect(login_url(database_url, "ingest", password))
    try:
        assert await login.fetchval("SELECT current_user") == "gaslit_ingest"
        assert await login.fetchval("SELECT pg_has_role('ingest_writer', 'USAGE')")
        for sql in (
            "SELECT count(*) FROM summary_records",  # no reading data
            "SELECT email FROM users",  # nor personal details
            "DROP TABLE summary_records",  # nor what only the worker's owner login may do
            "CREATE TABLE summary_records_p20000101 PARTITION OF summary_records "
            "FOR VALUES FROM ('2000-01-01T00:00:00+00') TO ('2000-01-02T00:00:00+00')",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await login.execute(sql)
    finally:
        await login.close()
