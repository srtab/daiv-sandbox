import io

import sentry_sdk
from fastapi import FastAPI
from pydantic import UUID4, Base64Bytes, BaseModel, Field

from .config import settings
from .sessions import SandboxDockerSession

if settings.SENTRY_DSN and settings.ENVIRONMENT != "local":
    sentry_sdk.init(dsn=str(settings.SENTRY_DSN), environment=settings.ENVIRONMENT, enable_tracing=True)

app = FastAPI(title=settings.PROJECT_NAME, openapi_url=f"{settings.API_V1_STR}/openapi.json")


class RunRequest(BaseModel):
    run_id: UUID4 = Field(..., description="Unique identifier for the run.")
    base_image: str = Field(..., description="Docker image to be used as the base image for the sandbox.")
    commands: list[str] = Field(
        ..., description="List of commands to be executed in the root directory of the archive."
    )
    archive: Base64Bytes = Field(..., description="Base64-encoded archive with files to be copied to the sandbox.")


class RunResponse(BaseModel):
    results: dict[str, dict[str, str | int]] = Field(..., description="Dictionary with the output of each command.")
    archive: Base64Bytes | None = Field(..., description="Base64-encoded archive with the changed files.")


@app.post("/run/commands/")
async def run_commands(request: RunRequest) -> RunResponse:
    """
    Run a set of commands in a sandboxed container and return archive with changed files.
    """
    run_dir = f"/tmp/run-{request.run_id}"  # noqa: S108
    results = {}

    with SandboxDockerSession(image=request.base_image, keep_template=True) as session:
        with io.BytesIO(request.archive) as archive:
            session.copy_to_runtime(run_dir, archive)

        for command in request.commands:
            result = session.execute_command(command, workdir=run_dir)
            results[command] = {"output": result.output.decode(), "exit_code": result.exit_code}

    return RunResponse(results=results, archive=session.extract_changed_files(run_dir))
