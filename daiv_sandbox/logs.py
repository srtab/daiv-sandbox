from fastapi_cli.utils.cli import get_uvicorn_log_config  # noqa: A005

from daiv_sandbox.config import settings

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
LOGGING_CONFIG["loggers"]["daiv_sandbox"] = {"level": settings.LOG_LEVEL, "handlers": ["verbose"]}
