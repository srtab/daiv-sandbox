from pydantic import UUID4, Base64Bytes, BaseModel, Field


class RunRequest(BaseModel):
    run_id: UUID4 = Field(..., description="Unique identifier for the run.")
    base_image: str = Field(..., description="Docker image to be used as the base image for the sandbox.")
    commands: list[str] = Field(..., description="List of commands to be executed in the sandbox.")
    workdir: str | None = Field(
        default=None,
        description=(
            "Working directory to be used for the commands. "
            "Defaults to the root directory where the archive is extracted."
        ),
    )
    archive: Base64Bytes = Field(..., description="Base64-encoded archive with files to be copied to the sandbox.")


class RunResult(BaseModel):
    command: str = Field(..., description="Command that was executed.")
    output: str = Field(..., description="Output of the command.")
    exit_code: int = Field(..., description="Exit code of the command.")
    changed_files: list[str] = Field(..., description="List of changed files.", exclude=True)


class RunResponse(BaseModel):
    results: list[RunResult] = Field(..., description="List of results of each command.")
    archive: str | None = Field(..., description="Base64-encoded archive with the changed files.")


class ForbiddenError(BaseModel):
    detail: str = Field(..., description="Error message.")
