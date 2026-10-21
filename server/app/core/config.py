import os
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

# Matches compose.yaml. Deployed environments always set DATABASE_URL from the secret store.
_DEV_DATABASE_URL = "postgresql://app:app@127.0.0.1:5432/app"
EDGE_SECRET_MIN = 32  # `openssl rand -hex 32` gives 64


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    # Prometheus metrics on their own port, never the public one (ADR 0021). None: off.
    metrics_port: int | None = None
    # Values of the header Cloudflare adds (ADR 0027); requests without one get 403. Empty: off,
    # as locally and until the domain goes through Cloudflare. Two during a rotation.
    edge_secrets: tuple[str, ...] = ()


def load_settings() -> Settings:
    url = os.environ.get("DATABASE_URL", _DEV_DATABASE_URL)
    # Fly sets FLY_APP_NAME on every machine, including the release command.
    if os.environ.get("FLY_APP_NAME"):
        _require_verified_tls(url)
    port = os.environ.get("METRICS_PORT")
    edge = tuple(os.environ.get("EDGE_SECRET", "").split())
    if any(len(s) < EDGE_SECRET_MIN for s in edge):
        # The message never includes the value.
        raise RuntimeError(f"EDGE_SECRET values must be at least {EDGE_SECRET_MIN} characters")
    return Settings(database_url=url, metrics_port=int(port) if port else None, edge_secrets=edge)


def _require_verified_tls(url: str) -> None:
    """Refuse to start unless the database certificate is checked (docs/security-config.md).

    asyncpg's sslmode=require encrypts but accepts any certificate, so whoever sits on the
    path could read the password and every query. The message never includes the URL.
    """
    if parse_qs(urlsplit(url).query).get("sslmode") != ["verify-full"]:
        raise RuntimeError("DATABASE_URL must set sslmode=verify-full when deployed")
