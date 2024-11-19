from typing import Literal

from get_docker_secret import get_docker_secret
from pydantic import HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True)

    API_V1_STR: str = "/api/v1"
    API_KEY: str = get_docker_secret("API_KEY")
    ENVIRONMENT: Literal["local", "staging", "production"] = "local"
    MAX_EXECUTION_TIME: int = 600  # seconds
    SENTRY_DSN: HttpUrl | None = None


settings = Settings()  # type: ignore
