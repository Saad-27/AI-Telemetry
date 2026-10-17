import pytest

from app.core.config import load_settings

NEON = "postgresql://u:secret-pw@ep-x.eu-central-1.aws.neon.tech/neondb"


@pytest.mark.parametrize("query", ["", "?sslmode=require", "?sslmode=verify-ca"])
def test_deployed_refuses_unverified_tls(monkeypatch: pytest.MonkeyPatch, query: str) -> None:
    monkeypatch.setenv("FLY_APP_NAME", "gaslit-api-production")
    monkeypatch.setenv("DATABASE_URL", NEON + query)
    with pytest.raises(RuntimeError) as exc:
        load_settings()
    assert "secret-pw" not in str(exc.value)


def test_deployed_accepts_verify_full(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLY_APP_NAME", "gaslit-api-production")
    monkeypatch.setenv("DATABASE_URL", NEON + "?sslmode=verify-full")
    assert load_settings().database_url.endswith("sslmode=verify-full")


def test_local_needs_no_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://app:app@postgres:5432/app")
    assert load_settings().database_url.endswith("/app")
