from typing import Literal

from pydantic import HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="DAIV_SANDBOX_")

    # Environment
    ENVIRONMENT: Literal["local", "staging", "production"] = "local"

    # API
    API_V1_STR: str = "/api/v1"
    API_KEY: str

    # Sentry
    SENTRY_DSN: HttpUrl | None = None
    SENTRY_CA_CERTS: str | None = None
    SENTRY_ENABLE_TRACING: bool = False

    # Execution
    MAX_EXECUTION_TIME: int = 600  # seconds
    RUNTIME: Literal["runc", "runsc"] = "runc"
    KEEP_TEMPLATE: bool = False


settings = Settings()  # type: ignore
