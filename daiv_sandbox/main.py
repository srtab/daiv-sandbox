import asyncio
import base64
import glob
import logging
import re
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Annotated, Literal

import sentry_sdk
from fastapi import Depends, FastAPI, HTTPException, Request, Response, Security, UploadFile, status
from fastapi import Path as FastAPIPath
from fastapi.security.api_key import APIKeyHeader
from redis.asyncio import Redis

from daiv_sandbox import __version__
from daiv_sandbox.config import settings
from daiv_sandbox.locks import NoopSessionLockManager, RedisSessionLockManager, SessionBusyError
from daiv_sandbox.logs import LOGGING_CONFIG
from daiv_sandbox.reaper import start_reaper
from daiv_sandbox.schemas import (
    ErrorMessage,
    FsDeleteRequest,
    FsDeleteResponse,
    FsEditRequest,
    FsEditResponse,
    FsEntry,
    FsError,
    FsErrorCode,
    FsGlobRequest,
    FsGlobResponse,
    FsGrepMatch,
    FsGrepRequest,
    FsGrepResponse,
    FsLsRequest,
    FsLsResponse,
    FsReadRequest,
    FsReadResponse,
    FsWriteRequest,
    FsWriteResponse,
    RunRequest,
    RunResponse,
    RunResult,
    StartSessionRequest,
    StartSessionResponse,
)
from daiv_sandbox.sessions import (
    DAIV_SANDBOX_TYPE_LABEL,
    SANDBOX_HOME,
    SANDBOX_ROOT,
    SKILLS_ROOT,
    TYPE_CMD_EXECUTOR,
    WORKSPACE_ROOT,
    SandboxDockerSession,
    SessionUnavailableError,
    _validate_sandbox_path,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

HEADER_API_KEY_NAME = "X-API-Key"

EXIT_CODE_TIMEOUT = 124  # matches timeout(1) convention

# One-shot seed guard marker. Lives in the sandbox home (outside /workspace) so it is container-local
# and not reachable through the fs/* endpoints; written/read via exec as root.
SEED_MARKER = f"{SANDBOX_HOME}/.daiv-seeded"

# Cap on the bytes a single fs/read returns. Mirrors deepagents BaseSandbox MAX_OUTPUT_BYTES /
# MAX_BINARY_BYTES. The full file is still read into the process (get_archive); this only bounds
# the response payload so a page of pathologically long lines can't produce an unbounded reply.
READ_MAX_OUTPUT_BYTES = 512_000

READ_TRUNCATION_MARKER = (
    f"\n\n[Output truncated: exceeded the {READ_MAX_OUTPUT_BYTES}-byte read limit. "
    "Continue with a larger offset or smaller limit to read the rest.]"
)


# Configure Sentry

if settings.SENTRY_DSN and settings.ENVIRONMENT != "local":
    sentry_sdk.init(
        dsn=str(settings.SENTRY_DSN),
        environment=settings.ENVIRONMENT,
        enable_logs=settings.SENTRY_ENABLE_LOGS,
        traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
        profiles_sample_rate=settings.SENTRY_PROFILES_SAMPLE_RATE,
        send_default_pii=settings.SENTRY_SEND_DEFAULT_PII,
        release=__version__,
    )

description = """\
FastAPI application designed to securely execute arbitrary commands within a controlled environment. Each execution is isolated in a Docker container with a secure execution space.

To enhance security, `daiv-sandbox` leverages [`gVisor`](https://github.com/google/gvisor) as its container runtime. This provides an additional layer of protection by restricting the running code's ability to interact with the host system, thereby minimizing the risk of sandbox escape.

While `gVisor` significantly improves security, it may introduce some performance overhead due to its additional isolation mechanisms. This trade-off is generally acceptable for applications prioritizing security over raw execution speed.
"""  # noqa: E501


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    redis_client: Redis | None = None

    if settings.REDIS_URL:
        redis_client = Redis.from_url(settings.REDIS_URL)
        app.state.redis = redis_client
        app.state.session_lock_manager = RedisSessionLockManager(
            redis_client,
            ttl_seconds=settings.SESSION_LOCK_TTL_SECONDS,
            wait_seconds=settings.SESSION_LOCK_WAIT_SECONDS,
            refresh_interval_seconds=settings.SESSION_LOCK_REFRESH_SECONDS,
        )
    else:
        if settings.ENVIRONMENT == "production":
            logger.warning(
                "REDIS_URL is not configured; per-session locking is disabled and concurrent requests may race"
            )
        app.state.redis = None
        app.state.session_lock_manager = NoopSessionLockManager()

    reaper_task = start_reaper(app)
    try:
        yield
    finally:
        if reaper_task is not None:
            reaper_task.cancel()
        if redis_client is not None:
            await redis_client.aclose()


app = FastAPI(
    debug=settings.ENVIRONMENT == "local",
    title="DAIV Runtime Sandbox",
    description=description,
    summary="Run commands in a secure environment.",
    version=__version__,
    license_info={"name": "Apache License 2.0", "url": "https://www.apache.org/licenses/LICENSE-2.0"},
    contact={"name": "DAIV", "url": "https://github.com/srtab/daiv-sandbox"},
    root_path=settings.API_V1_STR,
    lifespan=lifespan,
)
app.state.redis = None
app.state.session_lock_manager = NoopSessionLockManager()


@app.exception_handler(SessionBusyError)
async def _handle_session_busy(request: Request, exc: SessionBusyError) -> Response:
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": "Session is busy"})


@app.exception_handler(SessionUnavailableError)
async def _handle_session_unavailable(request: Request, exc: SessionUnavailableError) -> Response:
    # The container exists but a Docker fault prevented restarting/stopping it. This is an
    # infrastructure problem, not a missing session, so report 503 (retryable) rather than masking
    # it as a 404 or surfacing a bare 500.
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"detail": str(exc)})


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
    status.HTTP_409_CONFLICT: {
        "content": {"application/json": {"example": {"detail": "Session is busy"}}},
        "model": ErrorMessage,
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "content": {"application/json": {"example": {"detail": "Session could not be restarted"}}},
        "model": ErrorMessage,
    },
}

# fs/* endpoints surface all path/operation problems in the 200 body via the structured FsError,
# so they never return 400 — expose a 400-free variant rather than advertising an unreachable status.
_fs_responses = {code: resp for code, resp in common_responses.items() if code != status.HTTP_400_BAD_REQUEST}


@app.post("/session/", responses=common_responses, name="Obtain a session ID")
async def start_session(request: StartSessionRequest, api_key: str = Depends(get_api_key)) -> StartSessionResponse:
    """
    Start a session and return the session ID.

    A Docker container is created with the base image provided.
    The session ID is used to identify the created container in subsequent requests.

    This session ID ensures a consistent execution environment for the commands, including files and directories.
    """
    cmd_executor_labels = {DAIV_SANDBOX_TYPE_LABEL: TYPE_CMD_EXECUTOR}

    cmd_executor_kwargs = {}

    if not request.network_enabled:
        cmd_executor_kwargs["network_mode"] = "none"
    elif settings.NETWORK:
        cmd_executor_kwargs["network"] = settings.NETWORK

    if request.environment:
        cmd_executor_kwargs["environment"] = request.environment

    if request.memory_bytes:
        cmd_executor_kwargs["mem_limit"] = request.memory_bytes

    if request.cpus:
        cmd_executor_kwargs["cpus"] = request.cpus

    cmd_executor = await asyncio.to_thread(
        SandboxDockerSession.start, image=request.base_image, labels=cmd_executor_labels, **cmd_executor_kwargs
    )
    if cmd_executor.session_id is None:
        raise RuntimeError("Started session is missing a session ID")
    return StartSessionResponse(session_id=cmd_executor.session_id)


@app.post("/session/{session_id}/seed/", responses=common_responses, name="Seed initial session state")
async def seed_session(
    http_request: Request,
    session_id: Annotated[str, FastAPIPath(title="The ID of the session to seed.")],
    repo_archive: UploadFile | None = None,
    skills_archive: UploadFile | None = None,
    api_key: str = Depends(get_api_key),
) -> Response:
    """
    Establish the initial state of a freshly-started session.

    Multipart fields (at least one is required):
      * ``repo_archive``    — tar (auto-detected compression: gzip, bzip2, xz, zstd, or plain)
                              extracted into ``/workspace/repo``.
      * ``skills_archive``  — tar (same compression options) extracted into ``/workspace/skills``.

    One-shot per session: subsequent calls return 409.
    """
    if repo_archive is None and skills_archive is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="At least one of repo_archive or skills_archive must be provided",
        )

    async with http_request.app.state.session_lock_manager.acquire(session_id):
        cmd_executor = await asyncio.to_thread(SandboxDockerSession, session_id=session_id)

        if not cmd_executor.container:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found or already closed")

        check = await asyncio.to_thread(
            cmd_executor.container.exec_run, ["/bin/sh", "-c", f"test -f {SEED_MARKER}"], user="root"
        )
        if check.exit_code == 0:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Session already seeded")

        if repo_archive is not None:
            try:
                await asyncio.to_thread(
                    cmd_executor.copy_to_container, repo_archive.file, dest=SANDBOX_ROOT, clear_before_copy=False
                )
            except ValueError as exc:
                logger.warning("seed_session: invalid repo_archive for session %s: %s", session_id, exc)
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"repo_archive is invalid: {exc}"
                ) from exc

        if skills_archive is not None:
            try:
                await asyncio.to_thread(
                    cmd_executor.copy_to_container, skills_archive.file, dest=SKILLS_ROOT, clear_before_copy=False
                )
            except ValueError as exc:
                logger.warning("seed_session: invalid skills_archive for session %s: %s", session_id, exc)
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"skills_archive is invalid: {exc}"
                ) from exc

        marker_result = await asyncio.to_thread(
            cmd_executor.container.exec_run, ["/bin/sh", "-c", f"touch {SEED_MARKER}"], user="root"
        )
        if marker_result.exit_code != 0:
            logger.error("Failed to mark session as seeded: [%s] %s", marker_result.exit_code, marker_result.output)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to mark session as seeded. Check logs.",
            )

        return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/session/{session_id}/", responses=common_responses, name="Run commands on a session")
async def run_on_session(
    http_request: Request,
    session_id: Annotated[str, FastAPIPath(title="The ID of the session to run commands in.")],
    request: RunRequest,
    api_key: str = Depends(get_api_key),
) -> RunResponse:
    """
    Run a set of commands on a session and return each command's result.
    """
    async with http_request.app.state.session_lock_manager.acquire(session_id):
        cmd_executor = await asyncio.to_thread(SandboxDockerSession, session_id=session_id)

        if not cmd_executor.container:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found or already closed")

        raw_timeout = request.timeout if request.timeout is not None else settings.COMMAND_TIMEOUT
        effective_timeout = float(raw_timeout) if raw_timeout > 0 else None

        results: list[RunResult] = []
        for command in request.commands:
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(cmd_executor.execute_command, command), timeout=effective_timeout
                )
            except TimeoutError:
                # The underlying OS thread running exec_run cannot be interrupted and continues
                # until the Docker command finishes. Container state is indeterminate, so we stop
                # executing further commands. Resources are reclaimed when the session is closed.
                results.append(
                    RunResult(
                        command=command,
                        output=f"Command timed out after {effective_timeout:.0f}s",
                        exit_code=EXIT_CODE_TIMEOUT,
                        workdir=SANDBOX_ROOT,
                    )
                )
                break
            results.append(result)

            if request.fail_fast and result.exit_code != 0:
                break

        return RunResponse(results=results)


@app.delete("/session/{session_id}/", responses=common_responses, name="Close a session")
async def close_session(
    request: Request,
    session_id: Annotated[str, FastAPIPath(title="The ID of the session to close")],
    force: bool = False,
    api_key: str = Depends(get_api_key),
) -> Response:
    """
    Close a session. By default the container is *stopped* (preserved for warm reuse and reclaimed
    later by the reaper). Pass ``?force=true`` to remove it immediately.
    """
    async with request.app.state.session_lock_manager.acquire(session_id):
        cmd_executor = SandboxDockerSession()
        cmd_executor.session_id = session_id

        if force:
            await asyncio.to_thread(cmd_executor.remove_container)
        else:
            await asyncio.to_thread(cmd_executor.stop_container)

        return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/session/{session_id}/", responses=common_responses, name="Get session status")
async def get_session(
    request: Request,
    session_id: Annotated[str, FastAPIPath(title="The ID of the session to check")],
    api_key: str = Depends(get_api_key),
) -> Response:
    """
    Return 204 if the session's container exists (restarting it if stopped, i.e. warming it for
    reuse), or 404 if it does not. Lock-guarded so it can't race the reaper.
    """
    async with request.app.state.session_lock_manager.acquire(session_id):
        cmd_executor = await asyncio.to_thread(SandboxDockerSession, session_id=session_id)
        if not cmd_executor.container:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found or already closed")
        return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get(
    "/-/health/", responses={200: {"content": {"application/json": {"example": {"status": "ok"}}}}}, name="Healthcheck"
)
async def health() -> dict[Literal["status"], Literal["ok"]]:
    """
    Check if the Docker client is responding.
    """
    if not await asyncio.to_thread(SandboxDockerSession.ping):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Docker client is not responding")
    return {"status": "ok"}


@app.get("/-/version/", responses={200: {"content": {"application/json": {"example": {"version": __version__}}}}})
async def version() -> dict[Literal["version"], str]:
    """
    Get the version of the application.
    """
    return {"version": __version__}


# --- /workspace file-op endpoints -------------------------------------------
#
# These read/write/search files anywhere under WORKSPACE_ROOT (repo/, skills/, tmp/).
# They are Python-free (content via the Docker archive API, search/listing via POSIX
# grep/find/ls/rm), so they work on images without a Python interpreter (e.g. alpine).
# Edits land directly on the container's workspace, which is the single source of truth.

_WORKSPACE_ROOTS = (WORKSPACE_ROOT,)


def _validate_workspace_dir(path: str) -> None:
    """Validate a directory path for ls/grep/glob: the workspace root itself or anything under it.

    Raises ``ValueError`` (which the endpoints surface as a 200 body with ``error.code=invalid_path``,
    consistent with the file ops) on ``..`` traversal, NUL, newline, or a path that escapes
    WORKSPACE_ROOT. ``allow_root=True`` because ls/grep/glob legitimately target ``/workspace`` itself.
    """
    _validate_sandbox_path(path, allowed_roots=_WORKSPACE_ROOTS, allow_root=True)


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a path-aware glob (``**``, ``*``, ``?``, ``[..]``) to a regex for a base-relative path.

    Delegates to stdlib ``glob.translate`` (added in Python 3.13): ``recursive=True`` makes ``**/`` span
    directory boundaries while ``*`` stays within a single segment, and ``include_hidden=True`` lets
    ``*`` match dotfiles. This is intentionally NOT the same as the optional ``glob`` filter on
    ``fs_grep``, which uses ``fnmatch`` for a basename-only match — the two have deliberately
    different semantics, so don't try to merge them.
    """
    return re.compile(glob.translate(pattern, recursive=True, include_hidden=True))


@asynccontextmanager
async def _workspace_executor(http_request: Request, session_id: str) -> AsyncIterator[SandboxDockerSession]:
    """Acquire the session lock and yield a live cmd_executor, or 404."""
    async with http_request.app.state.session_lock_manager.acquire(session_id):
        cmd = await asyncio.to_thread(SandboxDockerSession, session_id=session_id)
        if not cmd.container:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found or already closed")
        yield cmd


@app.post("/session/{session_id}/fs/write", responses=_fs_responses, name="Write a workspace file")
async def fs_write(
    http_request: Request, session_id: str, request: FsWriteRequest, api_key: str = Depends(get_api_key)
) -> FsWriteResponse:
    try:
        _validate_sandbox_path(request.path, allowed_roots=_WORKSPACE_ROOTS)
    except ValueError as exc:
        return FsWriteResponse(error=FsError(code=FsErrorCode.INVALID_PATH, message=str(exc)))
    async with _workspace_executor(http_request, session_id) as cmd:
        try:
            await asyncio.to_thread(
                cmd.write_file,
                request.path,
                request.content,
                mode=request.mode,
                allowed_roots=_WORKSPACE_ROOTS,
                create_only=True,
            )
        except FileExistsError as exc:
            return FsWriteResponse(error=FsError(code=FsErrorCode.ALREADY_EXISTS, message=str(exc)))
        except RuntimeError as exc:
            # Expected operational failure (copy/probe). Any *other* exception (programming error,
            # Docker transport fault) is NOT caught here so it propagates to a real 500 — surfacing in
            # metrics/Sentry rather than hiding behind a 200 with an error body.
            logger.exception("fs_write failed for %s", request.path)
            return FsWriteResponse(error=FsError(code=FsErrorCode.EXEC_FAILED, message=str(exc)))
        return FsWriteResponse()


@app.post("/session/{session_id}/fs/read", responses=_fs_responses, name="Read a workspace file")
async def fs_read(
    http_request: Request, session_id: str, request: FsReadRequest, api_key: str = Depends(get_api_key)
) -> FsReadResponse:
    try:
        _validate_sandbox_path(request.path, allowed_roots=_WORKSPACE_ROOTS)
    except ValueError as exc:
        return FsReadResponse(error=FsError(code=FsErrorCode.INVALID_PATH, message=str(exc)))
    async with _workspace_executor(http_request, session_id) as cmd:
        try:
            raw = await asyncio.to_thread(cmd.read_file_bytes, request.path)
        except IsADirectoryError:
            return FsReadResponse(
                error=FsError(
                    code=FsErrorCode.IS_A_DIRECTORY, message=f"Is a directory: {request.path} (use fs/ls to list it)"
                )
            )
        except FileNotFoundError:
            return FsReadResponse(error=FsError(code=FsErrorCode.NOT_FOUND, message=f"No such file: {request.path}"))
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            if len(raw) > READ_MAX_OUTPUT_BYTES:
                return FsReadResponse(
                    error=FsError(
                        code=FsErrorCode.TOO_LARGE,
                        message=f"Binary file exceeds maximum preview size of {READ_MAX_OUTPUT_BYTES} bytes",
                    )
                )
            return FsReadResponse(content=base64.b64encode(raw).decode("ascii"), encoding="base64")
        if not text:
            return FsReadResponse(content="System reminder: File exists but has empty contents", encoding="utf-8")
        lines = text.splitlines()
        page = lines[request.offset : request.offset + request.limit]
        if request.offset and not page:
            return FsReadResponse(
                error=FsError(
                    code=FsErrorCode.INVALID_OFFSET,
                    message=f"Line offset {request.offset} exceeds file length ({len(lines)} lines)",
                )
            )
        content = "\n".join(page)
        encoded = content.encode("utf-8")
        if len(encoded) > READ_MAX_OUTPUT_BYTES:
            marker_bytes = len(READ_TRUNCATION_MARKER.encode("utf-8"))
            truncated = encoded[: READ_MAX_OUTPUT_BYTES - marker_bytes].decode("utf-8", errors="ignore")
            content = truncated + READ_TRUNCATION_MARKER
        return FsReadResponse(content=content, encoding="utf-8")


@app.post("/session/{session_id}/fs/ls", responses=_fs_responses, name="List a workspace directory")
async def fs_ls(
    http_request: Request, session_id: str, request: FsLsRequest, api_key: str = Depends(get_api_key)
) -> FsLsResponse:
    try:
        _validate_workspace_dir(request.path)
    except ValueError as exc:
        return FsLsResponse(error=FsError(code=FsErrorCode.INVALID_PATH, message=str(exc)))
    async with _workspace_executor(http_request, session_id) as cmd:
        try:
            entries = await asyncio.to_thread(cmd.list_dir, request.path)
        except FileNotFoundError:
            return FsLsResponse(error=FsError(code=FsErrorCode.NOT_FOUND, message=f"No such directory: {request.path}"))
        except NotADirectoryError:
            return FsLsResponse(
                error=FsError(code=FsErrorCode.NOT_A_DIRECTORY, message=f"Not a directory: {request.path}")
            )
        except PermissionError:
            return FsLsResponse(
                error=FsError(code=FsErrorCode.PERMISSION_DENIED, message=f"Permission denied: {request.path}")
            )
        except RuntimeError as exc:
            logger.exception("fs_ls failed for %s", request.path)
            return FsLsResponse(error=FsError(code=FsErrorCode.EXEC_FAILED, message=str(exc)))
        return FsLsResponse(entries=[FsEntry(path=p, is_dir=d) for p, d in entries])


@app.post("/session/{session_id}/fs/grep", responses=_fs_responses, name="Grep workspace files")
async def fs_grep(
    http_request: Request, session_id: str, request: FsGrepRequest, api_key: str = Depends(get_api_key)
) -> FsGrepResponse:
    try:
        _validate_workspace_dir(request.path)
    except ValueError as exc:
        return FsGrepResponse(error=FsError(code=FsErrorCode.INVALID_PATH, message=str(exc)))
    async with _workspace_executor(http_request, session_id) as cmd:
        try:
            excludes = (*settings.FS_PRUNE_DIRS, *request.exclude)
            matches = await asyncio.to_thread(cmd.grep, request.pattern, request.path, request.glob, excludes)
        except FileNotFoundError:
            return FsGrepResponse(error=FsError(code=FsErrorCode.NOT_FOUND, message=f"No such path: {request.path}"))
        except PermissionError:
            return FsGrepResponse(
                error=FsError(code=FsErrorCode.PERMISSION_DENIED, message=f"Permission denied: {request.path}")
            )
        except RuntimeError as exc:
            logger.exception("fs_grep failed for %s", request.path)
            return FsGrepResponse(error=FsError(code=FsErrorCode.EXEC_FAILED, message=str(exc)))
        return FsGrepResponse(matches=[FsGrepMatch(path=p, line=n, text=t) for p, n, t in matches])


@app.post("/session/{session_id}/fs/glob", responses=_fs_responses, name="Glob workspace files")
async def fs_glob(
    http_request: Request, session_id: str, request: FsGlobRequest, api_key: str = Depends(get_api_key)
) -> FsGlobResponse:
    try:
        _validate_workspace_dir(request.path)
    except ValueError as exc:
        return FsGlobResponse(error=FsError(code=FsErrorCode.INVALID_PATH, message=str(exc)))
    # glob.translate treats malformed bracket classes as literals (shell-like), so this never raises.
    regex = _glob_to_regex(request.pattern)
    async with _workspace_executor(http_request, session_id) as cmd:
        try:
            excludes = (*settings.FS_PRUNE_DIRS, *request.exclude)
            all_entries = await asyncio.to_thread(cmd.find_paths, request.path, excludes)
        except FileNotFoundError:
            return FsGlobResponse(
                error=FsError(code=FsErrorCode.NOT_FOUND, message=f"No such directory: {request.path}")
            )
        except NotADirectoryError:
            return FsGlobResponse(
                error=FsError(code=FsErrorCode.NOT_A_DIRECTORY, message=f"Not a directory: {request.path}")
            )
        except PermissionError:
            return FsGlobResponse(
                error=FsError(code=FsErrorCode.PERMISSION_DENIED, message=f"Permission denied: {request.path}")
            )
        except RuntimeError as exc:
            logger.exception("fs_glob failed for %s", request.path)
            return FsGlobResponse(error=FsError(code=FsErrorCode.EXEC_FAILED, message=str(exc)))
        base = request.path.rstrip("/")
        matched = [
            FsEntry(path=p, is_dir=d)
            for (p, d) in all_entries
            if p.startswith(f"{base}/") and regex.match(p[len(base) + 1 :])
        ]
        matched.sort(key=lambda e: e.path)
        return FsGlobResponse(matches=matched)


@app.post("/session/{session_id}/fs/edit", responses=_fs_responses, name="Edit a workspace file")
async def fs_edit(
    http_request: Request, session_id: str, request: FsEditRequest, api_key: str = Depends(get_api_key)
) -> FsEditResponse:
    try:
        _validate_sandbox_path(request.path, allowed_roots=_WORKSPACE_ROOTS)
    except ValueError as exc:
        return FsEditResponse(error=FsError(code=FsErrorCode.INVALID_PATH, message=str(exc)))
    async with _workspace_executor(http_request, session_id) as cmd:
        try:
            count = await asyncio.to_thread(
                cmd.edit_file,
                request.path,
                request.old,
                request.new,
                request.replace_all,
                allowed_roots=_WORKSPACE_ROOTS,
            )
        except IsADirectoryError:
            return FsEditResponse(
                error=FsError(code=FsErrorCode.IS_A_DIRECTORY, message=f"Is a directory: {request.path}")
            )
        except FileNotFoundError:
            return FsEditResponse(error=FsError(code=FsErrorCode.NOT_FOUND, message=f"No such file: {request.path}"))
        except UnicodeDecodeError:
            return FsEditResponse(
                error=FsError(code=FsErrorCode.NOT_A_TEXT_FILE, message=f"Not a text file: {request.path}")
            )
        except ValueError as exc:
            msg = str(exc)
            code = (
                FsErrorCode.MULTIPLE_OCCURRENCES if msg.startswith("String appears") else FsErrorCode.STRING_NOT_FOUND
            )
            return FsEditResponse(error=FsError(code=code, message=msg))
        return FsEditResponse(occurrences=count)


@app.post("/session/{session_id}/fs/delete", responses=_fs_responses, name="Delete a workspace file")
async def fs_delete(
    http_request: Request, session_id: str, request: FsDeleteRequest, api_key: str = Depends(get_api_key)
) -> FsDeleteResponse:
    try:
        _validate_sandbox_path(request.path, allowed_roots=_WORKSPACE_ROOTS)
    except ValueError as exc:
        return FsDeleteResponse(error=FsError(code=FsErrorCode.INVALID_PATH, message=str(exc)))
    async with _workspace_executor(http_request, session_id) as cmd:
        try:
            removed = await asyncio.to_thread(cmd.delete_file, request.path)
        except IsADirectoryError:
            return FsDeleteResponse(
                error=FsError(code=FsErrorCode.IS_A_DIRECTORY, message=f"Is a directory: {request.path}")
            )
        except RuntimeError as exc:
            logger.exception("fs_delete failed for %s", request.path)
            return FsDeleteResponse(error=FsError(code=FsErrorCode.EXEC_FAILED, message=str(exc)))
        return FsDeleteResponse(removed=removed)


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
