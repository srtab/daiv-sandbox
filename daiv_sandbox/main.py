import io
from pathlib import Path
from typing import Literal

import sentry_sdk
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader
from sentry_sdk.integrations.fastapi import FastApiIntegration
from starlette.status import HTTP_403_FORBIDDEN

from . import __version__
from .config import settings
from .schemas import RunRequest, RunResponse
from .sessions import SandboxDockerSession

API_KEY_NAME = "X-API-Key"


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
    title="DAIV Runtime Sandbox",
    description=description,
    summary="Run commands in a sandboxed container.",
    version=__version__,
    license_info={"name": "Apache License 2.0", "url": "https://www.apache.org/licenses/LICENSE-2.0"},
    contact={"name": "DAIV", "url": "https://github.com/srtab/daiv-sandbox"},
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
)


api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)


async def get_api_key(api_key_header: str | None = Security(api_key_header)) -> str:
    if api_key_header is None:
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="API Key header is missing")
    if api_key_header != settings.API_KEY:
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="Invalid API Key")
    return api_key_header


@app.post(f"{settings.API_V1_STR}/run/commands/")
async def run_commands(request: RunRequest, api_key: str = Depends(get_api_key)) -> RunResponse:
    """
    Run a set of commands in a sandboxed container and return archive with changed files.
    """
    run_dir = f"/tmp/run-{request.run_id}"  # noqa: S108
    results = {}

    with SandboxDockerSession(image=request.base_image, keep_template=True) as session:
        with io.BytesIO(request.archive) as archive:
            session.copy_to_runtime(run_dir, archive)

        command_workdir = Path(run_dir) / request.workdir if request.workdir else Path(run_dir)

        for command in request.commands:
            result = session.execute_command(command, workdir=command_workdir.as_posix())
            results[command] = {"output": result.output.decode(), "exit_code": result.exit_code}

    return RunResponse(results=results, archive=session.extract_changed_files(run_dir))


@app.get(f"{settings.API_V1_STR}/health/")
async def health() -> dict[Literal["status"], Literal["ok"]]:
    """
    Check if the service is healthy.
    """
    return {"status": "ok"}


@app.get(f"{settings.API_V1_STR}/version/")
async def version() -> dict[Literal["version"], str]:
    """
    Get the version of the service.
    """
    return {"version": __version__}
