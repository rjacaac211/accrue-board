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

    # Models per pipeline step (see docs/adr/0001). Override via environment variables.
    model_classify: str = "claude-haiku-4-5"
    model_extract: str = "claude-sonnet-5"
    model_verify: str = "claude-sonnet-5"
    model_code: str = "claude-sonnet-5"
    llm_mode: str = "auto"
    """Record/replay mode: live, record, replay or auto (see accrueboard.llm.client)."""
    recordings_dir: Path = Path(__file__).resolve().parents[3] / "data" / "recordings"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_cache_dir: Path = Path(__file__).resolve().parents[3] / "data" / "models"
    embedder: str = "fast"
    """fast = local ONNX model; hashing = dependency-free embedder (tests, offline demos)."""
    demo_mode: bool = False
    """Enables demo endpoints: fast-forwarding the shared clock and drip-feeding documents."""


@lru_cache
def get_settings() -> Settings:
    return Settings()
