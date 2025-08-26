import base64
import io
from typing import Annotated, Literal

import sentry_sdk
from fastapi import Depends, FastAPI, HTTPException, Path, Response, Security, status
from fastapi.security.api_key import APIKeyHeader

from daiv_sandbox import __version__
from daiv_sandbox.config import settings
from daiv_sandbox.logs import LOGGING_CONFIG
from daiv_sandbox.schemas import (
    ErrorMessage,
    RunRequest,
    RunResponse,
    RunResult,
    StartSessionRequest,
    StartSessionResponse,
)
from daiv_sandbox.sessions import SandboxDockerSession

HEADER_API_KEY_NAME = "X-API-Key"


# Configure Sentry

if settings.SENTRY_DSN and settings.ENVIRONMENT != "local":
    sentry_sdk.init(
        dsn=str(settings.SENTRY_DSN),
        environment=settings.ENVIRONMENT,
        enable_tracing=bool(settings.SENTRY_ENABLE_TRACING),
        profiles_sample_rate=1.0 if settings.SENTRY_ENABLE_TRACING else 0.0,
        release=__version__,
    )

description = """\
FastAPI application designed to securely execute arbitrary commands within a controlled environment. Each execution is isolated in a Docker container with a secure execution space.

To enhance security, `daiv-sandbox` leverages [`gVisor`](https://github.com/google/gvisor) as its container runtime. This provides an additional layer of protection by restricting the running code's ability to interact with the host system, thereby minimizing the risk of sandbox escape.

While `gVisor` significantly improves security, it may introduce some performance overhead due to its additional isolation mechanisms. This trade-off is generally acceptable for applications prioritizing security over raw execution speed.
"""  # noqa: E501

app = FastAPI(
    debug=settings.ENVIRONMENT == "local",
    title="DAIV Runtime Sandbox",
    description=description,
    summary="Run commands in a secure environment.",
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
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API Key header is missing")
    if api_key_header != settings.API_KEY.get_secret_value():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid API Key")
    return api_key_header


common_responses = {
    status.HTTP_403_FORBIDDEN: {
        "content": {"application/json": {"example": {"detail": "Invalid API Key"}}},
        "model": ErrorMessage,
    },
    status.HTTP_400_BAD_REQUEST: {
        "content": {"application/json": {"example": {"detail": "Error message"}}},
        "model": ErrorMessage,
    },
}


@app.post("/session/", responses=common_responses, name="Obtain a session ID")
async def start_session(request: StartSessionRequest, api_key: str = Depends(get_api_key)) -> StartSessionResponse:
    """
    Start a session and return the session ID.

    A Docker container is created with the base image or the Dockerfile provided.
    The session ID is used to identify the created container in subsequent requests.

    This session ID ensures a consistent execution environment for the commands, including files and directories.
    """
    session_id = SandboxDockerSession.start(image=request.base_image, dockerfile=request.dockerfile)
    return StartSessionResponse(session_id=session_id)


@app.post("/session/{session_id}/", responses=common_responses, name="Run commands on a session")
async def run_on_session(
    session_id: Annotated[str, Path(title="The ID of the session to run commands in.")],
    request: RunRequest,
    api_key: str = Depends(get_api_key),
) -> RunResponse:
    """
    Run a set of commands on a session and return the results, including the archive with changed files.
    """
    session = SandboxDockerSession()
    container = session.get_container(session_id)

    if not container:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    if request.archive:
        with io.BytesIO(request.archive) as request_archive:
            session.copy_to_container(container, request_archive)

    results: list[RunResult] = []
    for command in request.commands:
        result = session.execute_command(
            container, command, workdir=request.workdir, extract_changed_files=request.extract_changed_files
        )
        results.append(result)

        # Stop execution if fail_fast is enabled and command failed
        if request.fail_fast and result.exit_code != 0:
            break

    # Only create archive with changed files for the last command.
    archive: str | None = None

    if request.extract_changed_files and (changed_files := results[-1].changed_files):
        changed_files_archive = session.create_tar_gz_archive(container, results[-1].workdir, changed_files)
        archive = base64.b64encode(changed_files_archive.getvalue()).decode()

    return RunResponse(results=results, archive=archive)


@app.delete("/session/{session_id}/", responses=common_responses, name="Close a session")
async def close_session(
    session_id: Annotated[str, Path(title="The ID of the session to close")], api_key: str = Depends(get_api_key)
) -> Response:
    """
    Close a session by removing the Docker container.
    """
    SandboxDockerSession.end(session_id=session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get(
    "/-/health/", responses={200: {"content": {"application/json": {"example": {"status": "ok"}}}}}, name="Healthcheck"
)
async def health() -> dict[Literal["status"], Literal["ok"]]:
    """
    Check if the Docker client is responding.
    """
    if not SandboxDockerSession.ping():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Docker client is not responding")
    return {"status": "ok"}


@app.get("/-/version/", responses={200: {"content": {"application/json": {"example": {"version": __version__}}}}})
async def version() -> dict[Literal["version"], str]:
    """
    Get the version of the application.
    """
    return {"version": __version__}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "daiv_sandbox.main:app",
        host=settings.HOST,
        port=settings.PORT,
        log_config=LOGGING_CONFIG,
        reload=settings.ENVIRONMENT == "local",
        reload_dirs=["daiv_sandbox"],
    )
