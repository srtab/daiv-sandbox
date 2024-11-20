from typing import Literal

from pydantic import HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="DAIV_SANDBOX_")

    # API
    API_V1_STR: str = "/api/v1"
    API_KEY: str

    # Environment
    ENVIRONMENT: Literal["local", "staging", "production"] = "local"
    SENTRY_DSN: HttpUrl | None = None

    # Execution
    MAX_EXECUTION_TIME: int = 600  # seconds
    RUNTIME: Literal["runc", "runsc"] = "runc"
    KEEP_TEMPLATE: bool = False


settings = Settings()  # type: ignore
