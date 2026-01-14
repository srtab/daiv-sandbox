import warnings
from typing import Literal

from pydantic import HttpUrl, SecretStr  # noqa: TC002
from pydantic_settings import BaseSettings, SettingsConfigDict

warnings.filterwarnings(
    "ignore", message=r'directory "/run/secrets" does not exist', module="pydantic_settings.sources.providers.secrets"
)


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
    API_KEY: SecretStr

    # Sentry
    SENTRY_DSN: HttpUrl | None = None
    SENTRY_ENABLE_LOGS: bool = False
    SENTRY_TRACES_SAMPLE_RATE: float = 0.0
    SENTRY_PROFILES_SAMPLE_RATE: float = 0.0
    SENTRY_SEND_DEFAULT_PII: bool = False

    # Execution
    RUNTIME: Literal["runc", "runsc"] = "runc"
    RUN_UID: int = 1000
    RUN_GID: int = 1000

    # Git
    GIT_IMAGE: str = "alpine/git:2.49.1"


settings = Settings()  # type: ignore
