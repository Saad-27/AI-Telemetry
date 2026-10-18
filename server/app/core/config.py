import os
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

# Matches compose.yaml. Deployed environments always set DATABASE_URL from the secret store.
_DEV_DATABASE_URL = "postgresql://app:app@127.0.0.1:5432/app"


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    # Prometheus metrics on their own port, never the public one (ADR 0021). None: off.
    metrics_port: int | None = None


def load_settings() -> Settings:
    url = os.environ.get("DATABASE_URL", _DEV_DATABASE_URL)
    # Fly sets FLY_APP_NAME on every machine, including the release command.
    if os.environ.get("FLY_APP_NAME"):
        _require_verified_tls(url)
    port = os.environ.get("METRICS_PORT")
    return Settings(database_url=url, metrics_port=int(port) if port else None)


def _require_verified_tls(url: str) -> None:
    """Refuse to start unless the database certificate is checked (docs/security-config.md).

    asyncpg's sslmode=require encrypts but accepts any certificate, so whoever sits on the
    path could read the password and every query. The message never includes the URL.
    """
    if parse_qs(urlsplit(url).query).get("sslmode") != ["verify-full"]:
        raise RuntimeError("DATABASE_URL must set sslmode=verify-full when deployed")
