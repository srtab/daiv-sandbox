from typing import Literal

from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="DAIV_SANDBOX_", env_ignore_empty=True)

    # Server
    HOST: str = "0.0.0.0"  # noqa: S104
    PORT: int = 8000

    # Environment
    ENVIRONMENT: Literal["local", "production"] = "production"

    # Logging
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # API
    API_V1_STR: str = "/api/v1"
    API_KEY: str

    # Sentry
    SENTRY_DSN: HttpUrl | None = None
    SENTRY_ENABLE_TRACING: bool | int = False

    # Execution
    RUNTIME: Literal["runc", "runsc"] = "runc"
    KEEP_TEMPLATE: bool = False
    MAX_EXECUTION_TIME: int = Field(default=600, description="Maximum execution time in seconds")


settings = Settings()  # type: ignore
