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


# --- /scratch file-op wire schemas -----------------------------------------


class FsLsRequest(BaseModel):
    path: str = Field(description="Absolute directory path under /scratch.")


class FsEntry(BaseModel):
    path: str = Field(description="Absolute path of the entry.")
    is_dir: bool = Field(description="Whether the entry is a directory.")


class FsLsResponse(BaseModel):
    entries: list[FsEntry]


class FsReadRequest(BaseModel):
    path: str = Field(description="Absolute file path under /scratch.")
    offset: int = Field(default=0, ge=0, description="0-indexed start line (text files only).")
    limit: int = Field(default=2000, ge=1, description="Maximum number of lines (text files only).")


class FsReadResponse(BaseModel):
    content: str | None = Field(default=None, description="File content (utf-8 text or base64 binary).")
    encoding: str | None = Field(default=None, description='"utf-8" or "base64".')
    error: str | None = Field(default=None, description="Error message when the read failed.")


class FsGrepRequest(BaseModel):
    pattern: str = Field(description="Literal substring to search for (not a regex).")
    path: str = Field(description="Absolute directory/file path under /scratch.")
    glob: str | None = Field(default=None, description="Optional filename glob to restrict the search.")


class FsGrepMatch(BaseModel):
    path: str
    line: int
    text: str


class FsGrepResponse(BaseModel):
    matches: list[FsGrepMatch]


class FsGlobRequest(BaseModel):
    pattern: str = Field(description="Glob pattern (supports *, **, ?, [abc]).")
    path: str = Field(description="Absolute base directory under /scratch.")


class FsGlobResponse(BaseModel):
    matches: list[FsEntry]


class FsWriteRequest(BaseModel):
    path: str = Field(description="Absolute file path under /scratch.")
    content: Base64Bytes = Field(description="Base64-encoded full file content.")
    mode: int = Field(default=0o644, ge=0, le=0o7777, description="POSIX mode bits to set on the file.")


class FsWriteResponse(BaseModel):
    ok: bool
    error: str | None = None


class FsEditRequest(BaseModel):
    path: str = Field(description="Absolute file path under /scratch.")
    old: str = Field(description="Exact substring to replace.")
    new: str = Field(description="Replacement string.")
    replace_all: bool = Field(default=False, description="Replace every occurrence.")


class FsEditResponse(BaseModel):
    occurrences: int | None = Field(default=None, description="Number of replacements made.")
    error: str | None = Field(default=None, description="Error code/message on failure.")


class FsDeleteRequest(BaseModel):
    path: str = Field(description="Absolute file path under /scratch.")


class FsDeleteResponse(BaseModel):
    ok: bool
    error: str | None = None
