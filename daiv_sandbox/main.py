import base64
import io
from logging.config import dictConfig
from pathlib import Path
from typing import Literal

import sentry_sdk
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader
from sentry_sdk.integrations.fastapi import FastApiIntegration
from starlette.status import HTTP_403_FORBIDDEN

from . import __version__
from .config import settings
from .schemas import ForbiddenError, RunRequest, RunResponse, RunResult
from .sessions import SandboxDockerSession

HEADER_API_KEY_NAME = "X-API-Key"


# Configure root logger
dictConfig({
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "[%(asctime)s] %(levelname)s - %(name)s - %(message)s", "datefmt": "%d-%m-%Y:%H:%M:%S %z"}
    },
    "handlers": {"console": {"level": "DEBUG", "class": "logging.StreamHandler", "formatter": "verbose"}},
    "loggers": {
        "": {"level": "INFO", "handlers": ["console"]},
        "daiv_sandbox": {"level": "DEBUG", "handlers": ["console"], "propagate": False},
    },
})

if settings.SENTRY_DSN and settings.ENVIRONMENT != "local":
    sentry_sdk.init(
        dsn=str(settings.SENTRY_DSN),
        environment=settings.ENVIRONMENT,
        enable_tracing=True,
        release=__version__,
        integrations=[FastApiIntegration()],
    )

description = """
FastAPI application designed to securely execute arbitrary commands and untrusted code within a controlled environment. Each execution is isolated in a transient Docker container, which is automatically created and destroyed with every request, ensuring a clean and secure execution space.

To enhance security, `daiv-sandbox` leverages [`gVisor`](https://github.com/google/gvisor) as its container runtime. This provides an additional layer of protection by restricting the running code's ability to interact with the host system, thereby minimizing the risk of sandbox escape.

While `gVisor` significantly improves security, it may introduce some performance overhead due to its additional isolation mechanisms. This trade-off is generally acceptable for applications prioritizing security over raw execution speed.
"""  # noqa: E501

app = FastAPI(
    debug=settings.ENVIRONMENT == "local",
    title="DAIV Runtime Sandbox",
    description=description,
    summary="Run commands in a sandboxed container.",
    version=__version__,
    license_info={"name": "Apache License 2.0", "url": "https://www.apache.org/licenses/LICENSE-2.0"},
    contact={"name": "DAIV", "url": "https://github.com/srtab/daiv-sandbox"},
    root_path=settings.API_V1_STR,
)


api_key_header = APIKeyHeader(
    name=HEADER_API_KEY_NAME,
    auto_error=False,
    description=(
        "The API key must match the one declared in the DAIV Sandbox environment variables: `DAIV_SANDBOX_API_KEY`."
    ),
)


async def get_api_key(api_key_header: str | None = Security(api_key_header)) -> str:
    if api_key_header is None:
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="API Key header is missing")
    if api_key_header != settings.API_KEY:
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="Invalid API Key")
    return api_key_header


@app.post(
    "/run/commands/",
    responses={
        403: {"content": {"application/json": {"example": {"detail": "Invalid API Key"}}}, "model": ForbiddenError}
    },
)
async def run_commands(request: RunRequest, api_key: str = Depends(get_api_key)) -> RunResponse:
    """
    Run a set of commands in a sandboxed container and return archive with changed files.
    """
    results: list[RunResult] = []
    archive: str | None = None

    run_dir = f"/tmp/run-{request.run_id}"  # noqa: S108

    with SandboxDockerSession(
        image=request.base_image, keep_template=settings.KEEP_TEMPLATE, runtime=settings.RUNTIME
    ) as session:
        with io.BytesIO(request.archive) as request_archive:
            session.copy_to_runtime(run_dir, request_archive)

        command_workdir = Path(run_dir) / request.workdir if request.workdir else Path(run_dir)

        results = [session.execute_command(command, workdir=command_workdir.as_posix()) for command in request.commands]

        # Only create archive with changed files for the last command.
        if changed_files := results[-1].changed_files:
            changed_files_archive = session.create_tar_gz_archive(command_workdir, changed_files)
            archive = base64.b64encode(changed_files_archive.getvalue()).decode()

    return RunResponse(results=results, archive=archive)


@app.get("/health/", responses={200: {"content": {"application/json": {"example": {"status": "ok"}}}}})
async def health() -> dict[Literal["status"], Literal["ok"]]:
    """
    Check if the service is healthy.
    """
    return {"status": "ok"}


@app.get("/version/", responses={200: {"content": {"application/json": {"example": {"version": __version__}}}}})
async def version() -> dict[Literal["version"], str]:
    """
    Get the version of the service.
    """
    return {"version": __version__}
