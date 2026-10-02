import os
from dataclasses import dataclass

# Matches compose.yaml. Deployed environments always set DATABASE_URL from the secret store.
_DEV_DATABASE_URL = "postgresql://app:app@127.0.0.1:5432/app"


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str


def load_settings() -> Settings:
    return Settings(database_url=os.environ.get("DATABASE_URL", _DEV_DATABASE_URL))
