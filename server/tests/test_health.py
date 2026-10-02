from fastapi.testclient import TestClient

from app.core.config import Settings, load_settings
from app.main import create_app


def test_healthz() -> None:
    with TestClient(create_app()) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz_with_database() -> None:
    # Needs Postgres from `make test` (compose) or the CI service container.
    with TestClient(create_app(load_settings())) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 200


def test_readyz_without_database() -> None:
    unreachable = Settings(database_url="postgresql://nobody:x@127.0.0.1:1/none")
    with TestClient(create_app(unreachable)) as client:
        assert client.get("/healthz").status_code == 200
        resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json() == {"status": "unavailable"}
