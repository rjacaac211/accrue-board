"""Runtime configuration, read from environment variables (and a local .env file)."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = "postgresql+psycopg://accrue:accrue@localhost:5433/accrueboard"
    anthropic_api_key: str | None = None
    frontend_dist: str | None = None
    """Directory of the built frontend; served at / when set."""
    data_dir: Path = Path(__file__).resolve().parents[3] / "data"
    """Generated datasets and samples. Defaults to ``data/`` at the repository root."""


@lru_cache
def get_settings() -> Settings:
    return Settings()
