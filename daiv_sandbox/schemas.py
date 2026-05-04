from pydantic import Base64Bytes, BaseModel, Field


class StartSessionRequest(BaseModel):
    base_image: str = Field(description="Docker image to be used as the base image for the sandbox.")
    extract_patch: bool = Field(
        default=False, description="Whether to extract a patch with the changes made by the executed commands."
    )
    network_enabled: bool = Field(default=False, description="Whether to enable network for the sandbox.")
    environment: dict[str, str] | None = Field(
        default=None, description="Environment variables to set in the container at startup."
    )
    memory_bytes: int | None = Field(default=None, description="Memory in bytes to be used for the sandbox.")
    cpus: float | None = Field(default=None, description="CPUs to be used for the sandbox.")


class StartSessionResponse(BaseModel):
    session_id: str = Field(description="Unique identifier for the session.")


class RunRequest(BaseModel):
    commands: list[str] = Field(description="List of bash commands to be executed in the sandbox.")
    fail_fast: bool = Field(
        default=False,
        description=(
            "Stop execution immediately if any command fails (exit_code != 0). "
            "Defaults to False for backward compatibility."
        ),
    )
    timeout: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Maximum execution time in seconds for each command. "
            "Overrides the server default (DAIV_SANDBOX_COMMAND_TIMEOUT). "
            "0 means no timeout."
        ),
    )


class RunResult(BaseModel):
    command: str = Field(description="Command that was executed.")
    output: str = Field(description="Output of the command.")
    exit_code: int = Field(description="Exit code of the command.")
    workdir: str = Field(description="Working directory of the command.", exclude=True)


class RunResponse(BaseModel):
    results: list[RunResult] = Field(description="List of results of each command.")
    patch: str | None = Field(description="Base64-encoded patch with the changes.")


class PutMutation(BaseModel):
    path: str = Field(description="Absolute path inside the sandbox, must be under /repo.")
    content: Base64Bytes = Field(description="Base64-encoded full file content.")
    mode: int = Field(ge=0, le=0o7777, description="POSIX mode bits to set on the file.")


class ApplyMutationsRequest(BaseModel):
    mutations: list[PutMutation] = Field(min_length=1, max_length=64)


class MutationResult(BaseModel):
    path: str = Field(description="The path the mutation targeted.")
    ok: bool = Field(description="Whether the mutation was applied successfully.")
    error: str | None = Field(default=None, description="Per-item error message when ok=False.")


class ApplyMutationsResponse(BaseModel):
    results: list[MutationResult]


class ErrorMessage(BaseModel):
    detail: str = Field(description="Error message.")
