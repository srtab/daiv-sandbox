import uuid

from pydantic import Base64Bytes, BaseModel, Field, field_validator


def generate_session_id() -> str:
    return str(uuid.uuid4())


class StartSessionRequest(BaseModel):
    base_image: str | None = Field(
        default=None, description="Docker image to be used as the base image for the sandbox."
    )
    dockerfile: str | None = Field(default=None, description="Dockerfile content to be used to build the image.")
    extract_patch: bool = Field(
        default=False, description="Whether to extract a patch with the changes made by the executed commands."
    )

    @classmethod
    @field_validator("base_image", "dockerfile")
    def validate_base_image_or_dockerfile(cls, v, values):
        if not v and not values.get("dockerfile"):
            raise ValueError("Either base_image or dockerfile must be provided. Both cannot be None.")
        return v


class StartSessionResponse(BaseModel):
    session_id: str = Field(description="Unique identifier for the session.")


class RunRequest(BaseModel):
    commands: list[str] = Field(description="List of bash commands to be executed in the sandbox.")
    workdir: str | None = Field(
        default=None,
        description=(
            "Working directory to be used for the commands. "
            "Defaults to the root directory where the archive is extracted."
        ),
    )
    archive: Base64Bytes | None = Field(
        default=None, description="Base64-encoded archive with files to be copied to the sandbox."
    )
    fail_fast: bool = Field(
        default=False,
        description=(
            "Stop execution immediately if any command fails (exit_code != 0). "
            "Defaults to False for backward compatibility."
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


class ErrorMessage(BaseModel):
    detail: str = Field(description="Error message.")


class ImageAttrs(BaseModel):
    user: str = Field(description="User of the image.")
    working_dir: str = Field(description="Working directory of the image.")

    @classmethod
    def from_inspection(cls, inspection: dict) -> ImageAttrs:
        user = inspection["Config"]["User"]
        working_dir = inspection["Config"]["WorkingDir"]

        if user == "root" or working_dir == "" and user == "":
            working_dir = "/archives"

        return cls(user=user, working_dir=working_dir)
