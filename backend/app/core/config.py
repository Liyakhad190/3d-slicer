"""Application configuration.

Every setting is read from the environment. Secrets have no usable default outside
development: `validate_runtime` refuses to start if AUTH_SECRET is still the dev
value in a non-development environment (see ARCHITECTURE.md section 5).
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA_DIR = REPO_ROOT / "data"

_DEV_AUTH_SECRET = "dev-insecure-secret-change-me"  # noqa: S105 - dev-only sentinel
#: A relative SQLite path is resolved against `data_dir`, so this means
#: `<data_dir>/slicing.db`. Switch to
#: `postgresql+psycopg://user:pass@host:5432/slicing` for production.
_DEV_DATABASE_URL = "sqlite+pysqlite:///slicing.db"


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

    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )

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
    # `ollama` is the default and needs no API key: it is a local process.
    llm_provider: str = "ollama"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:1.5b"
    ollama_keep_alive: str = "15m"
    llm_timeout_seconds: int = 180
    llm_num_predict: int = 512
    llm_temperature: float = 0.0
    #: One retry on a transport or schema failure. Beyond that the caller gets a
    #: structured error; a weak local model must not stall a request.
    llm_max_retries: int = 1
    #: RAG
    rag_enabled: bool = True
    rag_max_documents: int = 6
    #: Model-directed tool calling. Off by default: the planner assembles tool
    #: context deterministically instead, which is both safer and far more
    #: reliable on a small local model. See docs/AI_ARCHITECTURE.md.
    llm_tool_calling_enabled: bool = False
    #: Only read when `llm_provider = "openai"`. Empty by design: the default
    #: path is a local Ollama and this platform ships no key in the repo.
    ai_api_key: str = ""
    ai_base_url: str = "https://api.openai.com/v1"
    ai_model: str = "gpt-4o-mini"

    # -- observability ---------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True
    sentry_dsn: str | None = None
    metrics_enabled: bool = True

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Accept a comma-separated list, which is how people write this.

        The `NoDecode` annotation on the field is what makes this reachable.
        Without it, pydantic-settings JSON-decodes the raw value first and
        raises on the documented `CORS_ORIGINS=http://localhost:5173`, because
        a bare origin is not valid JSON — which meant the README's own quick
        start produced a `.env` the app refused to start with. A JSON array is
        still honoured, since deployment tooling tends to emit one.
        """
        if not isinstance(value, str):
            return value
        text = value.strip()
        if text.startswith("["):
            try:
                decoded = json.loads(text)
            except ValueError:
                # Not JSON after all; fall through and treat it as a list.
                pass
            else:
                if isinstance(decoded, list):
                    return [str(item) for item in decoded]
        return [item.strip() for item in text.split(",") if item.strip()]

    @field_validator("data_dir", mode="after")
    @classmethod
    def _abs_data_dir(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @model_validator(mode="after")
    def _resolve_sqlite_path(self) -> "Settings":
        """Anchor a relative SQLite URL to the repo data directory.

        A bare `sqlite+pysqlite:///./data/slicing.db` silently depends on the
        process working directory, which differs between uvicorn, pytest and a
        worker started from elsewhere. Resolving it here means the same
        DATABASE_URL always means the same file.
        """
        url = self.database_url
        prefix = "sqlite+pysqlite:///"
        if url.startswith(prefix):
            raw_path = url[len(prefix) :]
            path = Path(raw_path)
            if not path.is_absolute():
                path = (self.data_dir / path).resolve()
            self.database_url = f"{prefix}{path.as_posix()}"
        return self

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
        self.data_dir.mkdir(parents=True, exist_ok=True)
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
        if self.is_production and not self.is_sqlite:
            if self.db_pool_size < 5:
                problems.append("DB_POOL_SIZE is too small for production traffic.")
        if self.llm_provider not in {"ollama", "openai"}:
            problems.append(
                f"LLM_PROVIDER {self.llm_provider!r} is not a known provider. "
                "Supported: ollama, openai."
            )
        if self.llm_provider == "openai" and not self.ai_api_key:
            problems.append("AI_API_KEY is required when LLM_PROVIDER=openai.")
        if not self.ollama_base_url.startswith(("http://", "https://")):
            problems.append("OLLAMA_BASE_URL must be an http(s) URL.")
        if not self.ollama_model.strip():
            problems.append("OLLAMA_MODEL must not be empty.")
        return problems


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Test hook: forces `get_settings` to re-read the environment."""
    get_settings.cache_clear()
