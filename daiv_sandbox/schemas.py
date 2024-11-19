from pydantic import UUID4, Base64Bytes, BaseModel, Field


class RunRequest(BaseModel):
    run_id: UUID4 = Field(..., description="Unique identifier for the run.")
    base_image: str = Field(..., description="Docker image to be used as the base image for the sandbox.")
    commands: list[str] = Field(..., description="List of commands to be executed in the sandbox.")
    workdir: str | None = Field(
        ...,
        description=(
            "Working directory to be used for the commands. "
            "Defaults to the root directory where the archive is extracted."
        ),
    )
    archive: Base64Bytes = Field(..., description="Base64-encoded archive with files to be copied to the sandbox.")


class RunResponse(BaseModel):
    results: dict[str, dict[str, str | int]] = Field(..., description="Dictionary with the output of each command.")
    archive: Base64Bytes | None = Field(..., description="Base64-encoded archive with the changed files.")
