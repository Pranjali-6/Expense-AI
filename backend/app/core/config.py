"""Application configuration.

Everything is environment-driven. Nothing here reads a file at runtime, and no
secret has a usable default — a missing secret should fail loudly at startup
rather than quietly fall back to something guessable.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ----------------------------------------------------------------- core --
    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    DEBUG: bool = False
    APP_NAME: str = "Expense AI"
    API_V1_PREFIX: str = "/api/v1"
    SERVICE_NAME: str = "api"

    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    LOG_FORMAT: Literal["json", "console"] = "json"

    # ------------------------------------------------------------- security --
    SECRET_KEY: str = Field(min_length=32)
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_TTL_MINUTES: int = 15
    REFRESH_TOKEN_TTL_DAYS: int = 30
    STORAGE_MASTER_KEK: str = Field(min_length=32)

    # Held as a raw string, exposed as a list via `cors_origins`.
    # pydantic-settings JSON-decodes any complex-typed field straight from the
    # environment, before field validators run — so a plain comma-separated
    # value on a `list[str]` field raises at import time.
    CORS_ORIGINS: str = "http://localhost"
    COOKIE_SECURE: bool = False
    COOKIE_DOMAIN: str | None = None
    CSRF_ENABLED: bool = True

    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_AUTH_PER_MINUTE: int = 10
    RATE_LIMIT_API_PER_MINUTE: int = 120
    RATE_LIMIT_UPLOAD_PER_HOUR: int = 60
    RATE_LIMIT_ASSISTANT_PER_MINUTE: int = 10

    # ----------------------------------------------------------- postgresql --
    POSTGRES_HOST: str = "postgres"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "expense_ai"
    POSTGRES_USER: str = "expense_owner"
    POSTGRES_PASSWORD: str = ""
    APP_DB_USER: str = "expense_app"
    APP_DB_PASSWORD: str = ""
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_ECHO: bool = False

    # ---------------------------------------------------------------- redis --
    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: str = ""
    CELERY_BROKER_DB: int = 1
    CELERY_RESULT_DB: int = 2

    # ---------------------------------------------------------------- minio --
    MINIO_ENDPOINT: str = "minio:9000"
    MINIO_PUBLIC_ENDPOINT: str = "http://localhost:9000"
    MINIO_ROOT_USER: str = "minio_admin"
    MINIO_ROOT_PASSWORD: str = ""
    MINIO_BUCKET_STATEMENTS: str = "statements"
    MINIO_BUCKET_EXPORTS: str = "exports"
    MINIO_SECURE: bool = False
    PRESIGNED_URL_TTL_SECONDS: int = 300

    # --------------------------------------------------------------- upload --
    MAX_UPLOAD_SIZE_MB: int = 25
    MAX_UPLOAD_FILES: int = 20
    MAX_PDF_PAGES: int = 300
    ALLOWED_UPLOAD_MIME: str = "application/pdf"

    CLAMAV_ENABLED: bool = False
    CLAMAV_HOST: str = "clamav"
    CLAMAV_PORT: int = 3310

    # ------------------------------------------------------------------ ocr --
    OCR_ENABLED: bool = True
    OCR_LANGUAGES: str = "eng"
    OCR_DPI: int = 300
    # Pages with fewer extractable characters than this are treated as scanned
    # and routed to OCR. A characters-per-page floor, not a density ratio:
    # "characters per square point" sounds more principled but produces numbers
    # like 0.001 for a perfectly readable statement, because a statement is a
    # sparse table rather than prose. A page with a real text layer clears 100
    # characters easily; a scan produces single digits.
    OCR_MIN_CHARS_PER_PAGE: int = 100

    # ------------------------------------------------------------------- ai --
    # The platform is fully functional with AI_ENABLED=false. See the plan:
    # the LLM is an enrichment component, never the source of truth.
    AI_ENABLED: bool = False
    AI_PROVIDER: Literal["gemini"] = "gemini"
    AI_MODEL_CATEGORIZE: str = "gemini-2.0-flash"
    AI_MODEL_ASSISTANT: str = "gemini-2.0-flash"
    AI_TIMEOUT_SECONDS: int = 20
    # A whole assistant exchange — up to five tool calls and the phrasing turn
    # — answers inside one HTTP request, so it needs a deadline of its own
    # rather than five times the per-call one.
    AI_ASSISTANT_TIMEOUT_SECONDS: int = 45
    AI_MAX_RETRIES: int = 2
    AI_MONTHLY_BUDGET_INR: Decimal = Decimal("500")
    GEMINI_API_KEY: str = ""

    # ----------------------------------------------------------- confidence --
    # Gate is min() of the four dimensions, never an average.
    CONFIDENCE_AUTO_APPROVE: Annotated[float, Field(ge=0, le=1)] = 0.97
    CONFIDENCE_REVIEW_REQUIRED: Annotated[float, Field(ge=0, le=1)] = 0.90

    # ----------------------------------------------------------- google auth --
    GOOGLE_OAUTH_ENABLED: bool = False
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost/api/v1/auth/oauth/google/callback"

    # -------------------------------------------------------- observability --
    METRICS_ENABLED: bool = True
    # Workers have no HTTP server of their own, so they start a small one just
    # for scraping. The API serves its metrics from the application port.
    WORKER_METRICS_PORT: int = 9100

    # ------------------------------------------------------------ retention --
    STATEMENT_RETENTION_DAYS: int = 2555
    AUDIT_LOG_RETENTION_DAYS: int = 2555
    JOB_EVENT_RETENTION_DAYS: int = 90

    # ------------------------------------------------------------ validators --
    @field_validator("COOKIE_DOMAIN", mode="before")
    @classmethod
    def _empty_domain_is_none(cls, value: object) -> object:
        return value or None

    # ------------------------------------------------------------- computed --
    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        """Async DSN for the application.

        Note this uses APP_DB_USER, not POSTGRES_USER. Connecting as the table
        owner would silently bypass every Row Level Security policy in the
        schema, which would make tenant isolation decorative.
        """
        return (
            f"postgresql+asyncpg://{self.APP_DB_USER}:{self.APP_DB_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def migration_database_url(self) -> str:
        """Sync DSN for Alembic, which runs as the owner so it can DDL."""
        return (
            f"postgresql+psycopg2://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    def _redis_url(self, db: int) -> str:
        auth = f":{self.REDIS_PASSWORD}@" if self.REDIS_PASSWORD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{db}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def redis_url(self) -> str:
        return self._redis_url(self.REDIS_DB)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def celery_broker_url(self) -> str:
        return self._redis_url(self.CELERY_BROKER_DB)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def celery_result_backend(self) -> str:
        return self._redis_url(self.CELERY_RESULT_DB)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def max_upload_bytes(self) -> int:
        return self.MAX_UPLOAD_SIZE_MB * 1024 * 1024

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ai_usable(self) -> bool:
        """AI is only usable when explicitly enabled *and* credentialed.

        Enabled-but-keyless is treated as disabled rather than as an error, so
        a misconfigured deployment degrades to deterministic categorization
        instead of failing uploads.
        """
        return self.AI_ENABLED and bool(self.GEMINI_API_KEY)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
