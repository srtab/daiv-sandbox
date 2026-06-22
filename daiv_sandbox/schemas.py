from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Base64Bytes, BaseModel, Field, SecretStr, computed_field, field_validator, model_validator


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


class FsErrorCode(StrEnum):
    INVALID_PATH = "invalid_path"
    NOT_FOUND = "not_found"
    NOT_A_DIRECTORY = "not_a_directory"
    IS_A_DIRECTORY = "is_a_directory"
    NOT_A_TEXT_FILE = "not_a_text_file"
    STRING_NOT_FOUND = "string_not_found"
    MULTIPLE_OCCURRENCES = "multiple_occurrences"
    ALREADY_EXISTS = "already_exists"
    TOO_LARGE = "too_large"
    INVALID_OFFSET = "invalid_offset"
    PERMISSION_DENIED = "permission_denied"
    EXEC_FAILED = "exec_failed"
    INVALID_PATTERN = "invalid_pattern"


class FsError(BaseModel):
    code: FsErrorCode = Field(description="Stable, machine-branchable error code.")
    message: str = Field(min_length=1, description="Human-readable hint the agent can act on.")


class FsLsRequest(BaseModel):
    path: str = Field(description="Absolute directory path under /workspace.")


class FsEntry(BaseModel):
    path: str = Field(description="Absolute path of the entry.")
    is_dir: bool = Field(description="Whether the entry is a directory.")


class FsLsResponse(BaseModel):
    entries: list[FsEntry] = Field(default_factory=list, description="Directory entries (empty on error).")
    error: FsError | None = Field(default=None, description="Structured error; null on success.")


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
    error: FsError | None = Field(default=None, description="Structured error; null on success.")


class FsGrepRequest(BaseModel):
    pattern: str = Field(description="Regular expression to search for (POSIX extended / ERE syntax).")
    path: str = Field(description="Absolute directory/file path under /workspace.")
    glob: str | None = Field(default=None, description="Optional filename glob to restrict the search.")
    exclude: list[str] = Field(
        default_factory=list,
        description="Directory basenames/globs to prune from the search (extends the server defaults).",
    )


class FsGrepMatch(BaseModel):
    path: str = Field(description="Absolute path of the matching file.")
    line: int = Field(description="1-indexed line number of the match.")
    text: str = Field(description="Text of the matching line.")


class FsGrepResponse(BaseModel):
    matches: list[FsGrepMatch] = Field(default_factory=list, description="Matches found (empty on error).")
    truncated: bool = Field(
        default=False, description="True when matches were capped server-side; narrow the search to see the rest."
    )
    error: FsError | None = Field(default=None, description="Structured error; null on success.")


class FsGlobRequest(BaseModel):
    pattern: str = Field(description="Glob pattern (supports *, **, ?, [abc]).")
    path: str = Field(description="Absolute base directory under /workspace.")
    exclude: list[str] = Field(
        default_factory=list,
        description="Directory basenames/globs to prune from the search (extends the server defaults).",
    )


class FsGlobResponse(BaseModel):
    matches: list[FsEntry] = Field(default_factory=list, description="Matching entries (empty on error).")
    error: FsError | None = Field(default=None, description="Structured error; null on success.")


class FsWriteRequest(BaseModel):
    path: str = Field(description="Absolute file path under /workspace.")
    content: Base64Bytes = Field(description="Base64-encoded full file content.")
    mode: int = Field(default=0o644, ge=0, le=0o7777, description="POSIX mode bits to set on the file.")


class FsWriteResponse(BaseModel):
    error: FsError | None = Field(default=None, description="Structured error; null on success.")

    @computed_field(description="True on success; derived from `error` (success ⇔ no error).")
    @property
    def ok(self) -> bool:
        return self.error is None


class FsEditRequest(BaseModel):
    path: str = Field(description="Absolute file path under /workspace.")
    old: str = Field(description="Exact substring to replace.")
    new: str = Field(description="Replacement string.")
    replace_all: bool = Field(default=False, description="Replace every occurrence.")


class FsEditResponse(BaseModel):
    occurrences: int | None = Field(default=None, description="Number of replacements made.")
    error: FsError | None = Field(default=None, description="Structured error; null on success.")


class FsDeleteRequest(BaseModel):
    path: str = Field(description="Absolute file path under /workspace.")


class FsDeleteResponse(BaseModel):
    removed: bool = Field(
        default=False, description="True if a file was actually removed; False if it was already absent."
    )
    error: FsError | None = Field(default=None, description="Structured error; null on success.")

    @computed_field(description="True on success; derived from `error` (success ⇔ no error).")
    @property
    def ok(self) -> bool:
        return self.error is None

    @model_validator(mode="after")
    def _check_removed_consistency(self) -> FsDeleteResponse:
        # A failed delete never removed anything: keep `removed` and `error` from contradicting.
        if self.error is not None and self.removed:
            raise ValueError("removed must be False when an error is present")
        return self


# --- Egress proxy wire schemas -----------------------------------------------


class EgressRule(BaseModel):
    host: str = Field(description="Destination host glob (e.g. 'github.com', '*.githubusercontent.com').")
    methods: list[str] = Field(default_factory=lambda: ["*"], description="Allowed HTTP methods, or ['*'] for any.")
    inject: str | None = Field(default=None, description="Name of the secret whose header is injected for this host.")

    @field_validator("methods", mode="after")
    @classmethod
    def _upper(cls, value: list[str]) -> list[str]:
        return [m.upper() for m in value]


class EgressSecret(BaseModel):
    header: str = Field(description="Header name to set (e.g. 'Authorization', 'PRIVATE-TOKEN').")
    value: SecretStr = Field(description="Header value; redacted in logs/repr.")


class EgressPolicy(BaseModel):
    default: Literal["deny", "allow"] = Field(default="deny", description="Reachability for unlisted hosts.")
    intercept: Literal["all", "credentialed"] = Field(
        default="all", description="'all' MITMs every reachable host; 'credentialed' MITMs only inject hosts."
    )
    rules: list[EgressRule] = Field(default_factory=list)


class EgressConfigRequest(BaseModel):
    policy: EgressPolicy = Field(default_factory=EgressPolicy)
    secrets: dict[str, EgressSecret] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _injects_resolve(self) -> EgressConfigRequest:
        for rule in self.policy.rules:
            if rule.inject is not None and rule.inject not in self.secrets:
                raise ValueError(f"rule for host {rule.host!r} references unknown secret {rule.inject!r}")
        return self


class EgressConfigResponse(BaseModel):
    ok: bool = Field(default=True, description="True when the policy was provisioned to the sidecar.")
