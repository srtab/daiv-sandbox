import logging
from typing import Any

from fastapi_cli.utils.cli import get_uvicorn_log_config  # noqa: A005

from daiv_sandbox.config import settings


class EndpointFilter(logging.Filter):
    def __init__(self, path: str, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._path = path

    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage().find(self._path) == -1


# Configure logger


LOGGING_CONFIG = get_uvicorn_log_config()
LOGGING_CONFIG["formatters"]["verbose"] = {
    "format": "[%(asctime)s] %(levelname)s - %(name)s - %(message)s",
    "datefmt": "%d-%m-%Y:%H:%M:%S %z",
}
LOGGING_CONFIG["handlers"]["verbose"] = {
    "formatter": "verbose",
    "class": "logging.StreamHandler",
    "stream": "ext://sys.stderr",
}
LOGGING_CONFIG["filters"] = {"ignore_health": {"()": EndpointFilter, "path": "/-/health/"}}
LOGGING_CONFIG["loggers"]["daiv_sandbox"] = {"level": settings.LOG_LEVEL, "handlers": ["verbose"]}
LOGGING_CONFIG["loggers"]["uvicorn.access"]["filters"] = ["ignore_health"]
