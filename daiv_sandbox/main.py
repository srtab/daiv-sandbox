import base64
import io
import logging
from typing import Annotated, Literal

import sentry_sdk
from fastapi import Depends, FastAPI, HTTPException, Response, Security, status
from fastapi import Path as FastAPIPath
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

logger = logging.getLogger(__name__)

HEADER_API_KEY_NAME = "X-API-Key"

DAIV_SANDBOX_TYPE_LABEL = "daiv.sandbox.type"
DAIV_SANDBOX_PATCH_EXTRACTOR_SESSION_ID_LABEL = "daiv.sandbox.patch_extractor_session_id"

TYPE_PATCH_EXTRACTOR = "patch_extractor"
TYPE_CMD_EXECUTOR = "cmd_executor"

CMD_GIT_CONFIG_SAFE_DIRECTORY = "git config --global --add safe.directory {workdir}"
CMD_GIT_DIFF_BINARY = "git -c core.quotepath=false diff --binary HEAD"


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
    cmd_labels = {DAIV_SANDBOX_TYPE_LABEL: TYPE_CMD_EXECUTOR}

    if request.extract_patch:
        patch_extractor = SandboxDockerSession.start(
            image=settings.GIT_IMAGE, labels={DAIV_SANDBOX_TYPE_LABEL: TYPE_PATCH_EXTRACTOR}, network_mode="none"
        )
        cmd_labels[DAIV_SANDBOX_PATCH_EXTRACTOR_SESSION_ID_LABEL] = patch_extractor.session_id

    cmd_executor = SandboxDockerSession.start(
        image=request.base_image, dockerfile=request.dockerfile, labels=cmd_labels
    )
    return StartSessionResponse(session_id=cmd_executor.session_id)


@app.post("/session/{session_id}/", responses=common_responses, name="Run commands on a session")
async def run_on_session(
    session_id: Annotated[str, FastAPIPath(title="The ID of the session to run commands in.")],
    request: RunRequest,
    api_key: str = Depends(get_api_key),
) -> RunResponse:
    """
    Run a set of commands on a session and return the results, including the patch of the changed files if
    the `extract_patch` parameter is set to `true` in the request to start the session and there were changes
    made by the commands.
    """
    cmd_executor = SandboxDockerSession(session_id=session_id)

    if not cmd_executor.container:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found or already closed")

    # Patch with the changes applied by the commands.
    base64_patch: str | None = None
    # Results of the commands.
    results: list[RunResult] = []

    if request.archive:
        cmd_executor.copy_to_container(io.BytesIO(request.archive))

    for command in request.commands:
        result = cmd_executor.execute_command(command, workdir=request.workdir)
        results.append(result)

        # Stop execution if fail_fast is enabled and command failed
        if request.fail_fast and result.exit_code != 0:
            break

    if request.archive and (
        extract_patch_session_id := cmd_executor.get_label(DAIV_SANDBOX_PATCH_EXTRACTOR_SESSION_ID_LABEL)
    ):
        patch_workdir = f"/workdir/{request.workdir}"

        patch_extractor = SandboxDockerSession(session_id=extract_patch_session_id)
        patch_extractor.copy_to_container(cmd_executor.copy_from_container(request.workdir), dest="/workdir")
        patch_extractor.execute_command(
            CMD_GIT_CONFIG_SAFE_DIRECTORY.format(workdir=patch_workdir), workdir=patch_workdir
        )
        patch_result = patch_extractor.execute_command(CMD_GIT_DIFF_BINARY, workdir=patch_workdir)

        if patch_result.exit_code != 0:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to extract patch. Confirm that the archive is a git repository.",
            )

        base64_patch = base64.b64encode(patch_result.output.encode()).decode()

    return RunResponse(results=results, patch=base64_patch)


@app.delete("/session/{session_id}/", responses=common_responses, name="Close a session")
async def close_session(
    session_id: Annotated[str, FastAPIPath(title="The ID of the session to close")], api_key: str = Depends(get_api_key)
) -> Response:
    """
    Close a session by removing the Docker container.
    """
    cmd_executor = SandboxDockerSession(session_id=session_id)

    if patch_extractor_session_id := cmd_executor.get_label(DAIV_SANDBOX_PATCH_EXTRACTOR_SESSION_ID_LABEL):
        patch_extractor = SandboxDockerSession(session_id=patch_extractor_session_id)
        patch_extractor.remove_container()

    cmd_executor.remove_container()

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
