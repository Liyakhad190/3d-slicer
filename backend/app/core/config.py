"""Application configuration.

Every setting is read from the environment. Secrets have no usable default outside
development: `validate_runtime` refuses to start if AUTH_SECRET is still the dev
value in a non-development environment (see ARCHITECTURE.md section 5).
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA_DIR = REPO_ROOT / "data"

_DEV_AUTH_SECRET = "dev-insecure-secret-change-me"  # noqa: S105 - dev-only sentinel
_DEV_DATABASE_URL = "sqlite+pysqlite:///./data/slicing.db"


class Settings(BaseSettings):
    """Typed application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
    )

    # -- runtime ---------------------------------------------------------------
    environment: Literal["development", "test", "staging", "production"] = "development"
    debug: bool = True
    app_name: str = "slicing-platform"
    api_prefix: str = "/api/v1"

    # -- storage ---------------------------------------------------------------
    data_dir: Path = DEFAULT_DATA_DIR
    blob_dir_name: str = "blobs"
    max_upload_bytes: int = 200 * 1024 * 1024  # 200 MB
    max_archive_ratio: int = 120  # zip-bomb guard for 3MF/AMF
    max_archive_entries: int = 20_000

    # -- database --------------------------------------------------------------
    database_url: str = _DEV_DATABASE_URL
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_echo: bool = False

    # -- security --------------------------------------------------------------
    auth_secret: str = _DEV_AUTH_SECRET
    auth_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 60 * 60 * 12
    argon2_time_cost: int = 3
    argon2_memory_cost_kib: int = 65_536
    argon2_parallelism: int = 4

    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    # -- rate limiting ---------------------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_requests: int = 240
    rate_limit_window_seconds: int = 60
    upload_rate_limit_requests: int = 20
    upload_rate_limit_window_seconds: int = 300

    # -- jobs / workers --------------------------------------------------------
    job_worker_enabled: bool = False
    job_worker_id: str = "worker-local"
    job_lease_seconds: int = 300
    job_max_attempts: int = 3
    job_queue_max_depth: int = 5_000

    # -- slicing ---------------------------------------------------------------
    cura_engine_path: Path = Path(
        r"C:\Program Files\UltiMaker Cura 5.13.0\CuraEngine.exe"
    )
    slicer_timeout_seconds: int = 900
    slicer_max_output_bytes: int = 512 * 1024 * 1024

    # -- ai --------------------------------------------------------------------
    ai_api_key: str | None = None
    ai_provider: str = "disabled"  # disabled | openai_compatible
    ai_base_url: str | None = None
    ai_model: str = "gpt-4o-mini"
    ai_timeout_seconds: int = 60
    ai_max_tool_iterations: int = 12

    # -- observability ---------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True
    sentry_dsn: str | None = None
    metrics_enabled: bool = True

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("data_dir", mode="after")
    @classmethod
    def _abs_data_dir(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @property
    def blob_dir(self) -> Path:
        return self.data_dir / self.blob_dir_name

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_production(self) -> bool:
        return self.environment in {"staging", "production"}

    def validate_runtime(self) -> list[str]:
        """Return a list of startup errors. Empty means safe to serve traffic."""
        problems: list[str] = []
        if self.is_production:
            if self.auth_secret == _DEV_AUTH_SECRET:
                problems.append(
                    "AUTH_SECRET is still the development default. Set AUTH_SECRET "
                    "to a high-entropy value (>=32 bytes) before starting."
                )
            if len(self.auth_secret) < 32:
                problems.append("AUTH_SECRET must be at least 32 characters in production.")
            if self.debug:
                problems.append("DEBUG must be false in production.")
            if self.ai_api_key is None and self.ai_provider != "disabled":
                problems.append("AI_API_KEY is required unless AI_PROVIDER=disabled.")
        if self.is_production and not self.is_sqlite:
            if self.db_pool_size < 5:
                problems.append("DB_POOL_SIZE is too small for production traffic.")
        return problems


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Test hook: forces `get_settings` to re-read the environment."""
    get_settings.cache_clear()
