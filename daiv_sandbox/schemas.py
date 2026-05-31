from typing import Literal

from pydantic import Base64Bytes, BaseModel, Field


class StartSessionRequest(BaseModel):
    base_image: str = Field(description="Docker image to be used as the base image for the sandbox.")
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


class ErrorMessage(BaseModel):
    detail: str = Field(description="Error message.")


# --- /workspace file-op wire schemas -----------------------------------------


class FsLsRequest(BaseModel):
    path: str = Field(description="Absolute directory path under /workspace.")


class FsEntry(BaseModel):
    path: str = Field(description="Absolute path of the entry.")
    is_dir: bool = Field(description="Whether the entry is a directory.")


class FsLsResponse(BaseModel):
    entries: list[FsEntry] = Field(default_factory=list, description="Directory entries (empty on error).")
    error: str | None = Field(default=None, description="Error message when the listing failed.")


class FsReadRequest(BaseModel):
    path: str = Field(description="Absolute file path under /workspace.")
    offset: int = Field(default=0, ge=0, description="0-indexed start line (text files only).")
    limit: int = Field(default=2000, ge=1, description="Maximum number of lines (text files only).")


class FsReadResponse(BaseModel):
    content: str | None = Field(
        default=None,
        description=(
            "File content (utf-8 text or base64 binary). For an empty file this is a human-readable "
            "sentinel string (with encoding 'utf-8'), not the file's bytes."
        ),
    )
    encoding: Literal["utf-8", "base64"] | None = Field(
        default=None, description="Encoding of `content`: 'utf-8' for text, 'base64' for binary."
    )
    error: str | None = Field(default=None, description="Error message when the read failed.")


class FsGrepRequest(BaseModel):
    pattern: str = Field(description="Literal substring to search for (not a regex).")
    path: str = Field(description="Absolute directory/file path under /workspace.")
    glob: str | None = Field(default=None, description="Optional filename glob to restrict the search.")


class FsGrepMatch(BaseModel):
    path: str = Field(description="Absolute path of the matching file.")
    line: int = Field(description="1-indexed line number of the match.")
    text: str = Field(description="Text of the matching line.")


class FsGrepResponse(BaseModel):
    matches: list[FsGrepMatch] = Field(default_factory=list, description="Matches found (empty on error).")
    error: str | None = Field(default=None, description="Error message when the search failed.")


class FsGlobRequest(BaseModel):
    pattern: str = Field(description="Glob pattern (supports *, **, ?, [abc]).")
    path: str = Field(description="Absolute base directory under /workspace.")


class FsGlobResponse(BaseModel):
    matches: list[FsEntry] = Field(default_factory=list, description="Matching entries (empty on error).")
    error: str | None = Field(default=None, description="Error message when the glob failed.")


class FsWriteRequest(BaseModel):
    path: str = Field(description="Absolute file path under /workspace.")
    content: Base64Bytes = Field(description="Base64-encoded full file content.")
    mode: int = Field(default=0o644, ge=0, le=0o7777, description="POSIX mode bits to set on the file.")


class FsWriteResponse(BaseModel):
    ok: bool
    error: str | None = None


class FsEditRequest(BaseModel):
    path: str = Field(description="Absolute file path under /workspace.")
    old: str = Field(description="Exact substring to replace.")
    new: str = Field(description="Replacement string.")
    replace_all: bool = Field(default=False, description="Replace every occurrence.")


class FsEditResponse(BaseModel):
    occurrences: int | None = Field(default=None, description="Number of replacements made.")
    error: str | None = Field(default=None, description="Error code/message on failure.")


class FsDeleteRequest(BaseModel):
    path: str = Field(description="Absolute file path under /workspace.")


class FsDeleteResponse(BaseModel):
    ok: bool
    error: str | None = None
