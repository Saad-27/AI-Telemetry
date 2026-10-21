"""Requests that skip Cloudflare are refused (ADR 0027)."""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.core.config import Settings, load_settings, require_edge_secret
from app.main import create_app

SECRET = "a" * 64
OLD = "b" * 64
DEAD_DB = "postgresql://nobody:x@127.0.0.1:1/none"


def _app(*secrets: str) -> FastAPI:
    app = create_app(Settings(database_url=DEAD_DB, edge_secrets=secrets))

    @app.get("/headers")
    async def headers(request: Request) -> list[str]:
        return list(request.headers)

    return app


def test_refuses_requests_without_the_header() -> None:
    with TestClient(_app(SECRET)) as client:
        for resp in (
            client.get("/readyz"),
            client.post("/v1/ingest", content=b"{}"),
            client.get("/readyz", headers={"x-gaslit-edge": "wrong"}),
            client.get("/readyz", headers={"x-gaslit-edge": SECRET[:-1]}),
        ):
            assert resp.status_code == 403
            assert resp.json() == {"error": "forbidden"}


def test_lets_cloudflare_through_and_hides_the_secret() -> None:
    with TestClient(_app(SECRET)) as client:
        # Reaches the app: the database is down, so 503 rather than 403.
        assert client.get("/readyz", headers={"X-Gaslit-Edge": SECRET}).status_code == 503
        resp = client.get("/headers", headers={"x-gaslit-edge": SECRET, "x-other": "1"})
    assert resp.status_code == 200
    assert "x-other" in resp.json()
    assert "x-gaslit-edge" not in resp.json()


def test_healthz_stays_open_for_fly_checks() -> None:
    with TestClient(_app(SECRET)) as client:
        assert client.get("/healthz").status_code == 200


def test_rotation_accepts_both_values() -> None:
    with TestClient(_app(SECRET, OLD)) as client:
        for value in (SECRET, OLD):
            assert client.get("/headers", headers={"x-gaslit-edge": value}).status_code == 200


def test_off_without_secrets() -> None:
    with TestClient(_app()) as client:
        assert client.get("/headers").status_code == 200


def test_settings_read_edge_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    monkeypatch.delenv("EDGE_SECRET", raising=False)
    assert load_settings().edge_secrets == ()
    monkeypatch.setenv("EDGE_SECRET", f"{SECRET} {OLD}")
    assert load_settings().edge_secrets == (SECRET, OLD)


def test_settings_refuse_short_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    monkeypatch.setenv("EDGE_SECRET", f"{SECRET} short-sentinel")
    with pytest.raises(RuntimeError) as exc:
        load_settings()
    assert "short-sentinel" not in str(exc.value)


def test_deployed_app_requires_the_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLY_APP_NAME", "gaslit-api-production")
    with pytest.raises(RuntimeError):
        create_app(Settings(database_url=DEAD_DB))
    require_edge_secret(Settings(database_url=DEAD_DB, edge_secrets=(SECRET,)))
    monkeypatch.delenv("FLY_APP_NAME")
    require_edge_secret(Settings(database_url=DEAD_DB))  # locally and in tests: optional
