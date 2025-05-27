from typing import Literal

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
    fail_fast: bool = Field(
        default=False,
        description="Stop execution immediately if any command fails (exit_code != 0). Defaults to False for backward compatibility.",
    )


class RunResult(BaseModel):
    command: str = Field(..., description="Command that was executed.")
    output: str = Field(..., description="Output of the command.")
    exit_code: int = Field(..., description="Exit code of the command.")
    workdir: str = Field(..., description="Working directory of the command.", exclude=True)
    changed_files: list[str] = Field(default_factory=list, description="List of changed files.", exclude=True)


class RunResponse(BaseModel):
    results: list[RunResult] = Field(..., description="List of results of each command.")
    archive: str | None = Field(..., description="Base64-encoded archive with the changed files.")


class ErrorMessage(BaseModel):
    detail: str = Field(..., description="Error message.")


class RunCodeRequest(BaseModel):
    run_id: UUID4 = Field(..., description="Unique identifier for the run.")
    language: Literal["python"] = Field(..., description="Language to be used for the code execution.")
    dependencies: list[str] = Field(
        default_factory=list, description="List of dependencies to be installed in the sandbox."
    )
    code: str = Field(..., description="Code to be executed.")


class RunCodeResponse(BaseModel):
    output: str = Field(..., description="Output of the code execution.")
