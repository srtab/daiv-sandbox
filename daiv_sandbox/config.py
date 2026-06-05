import warnings
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr  # noqa: TC002
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
    COMMAND_TIMEOUT: int = Field(default=0, ge=0)  # per-command timeout in seconds; 0 = no timeout
    # Network attached to cmd-executor containers when a session is network-enabled. None -> Docker's
    # default bridge (no compose-service DNS). Set to a compose/user-defined network (e.g.
    # "daiv_default") so containers can resolve & reach sibling services like "gitlab:8929".
    NETWORK: str | None = None

    # Session locking
    REDIS_URL: str | None = None
    SESSION_LOCK_TTL_SECONDS: int = 900
    SESSION_LOCK_WAIT_SECONDS: float = 1.0
    SESSION_LOCK_REFRESH_SECONDS: float = 30.0

    # Session reaper / lifecycle
    REAPER_ENABLED: bool = True
    REAPER_INTERVAL_SECONDS: int = Field(default=600, gt=0)  # sweep cadence in seconds
    SESSION_GRACE_SECONDS: int = Field(default=43200, ge=0)  # stopped -> removed age (12h)
    MAX_STOPPED_SESSIONS: int = Field(default=50, ge=0)  # LRU cap on retained stopped containers
    STOP_TIMEOUT_SECONDS: int = Field(default=2, ge=0)  # docker stop grace before SIGKILL


settings = Settings()  # type: ignore
