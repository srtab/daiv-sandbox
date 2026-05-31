import asyncio
import base64
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
from daiv_sandbox.schemas import (
    ApplyMutationsRequest,
    ApplyMutationsResponse,
    ErrorMessage,
    FsDeleteRequest,
    FsDeleteResponse,
    FsEditRequest,
    FsEditResponse,
    FsEntry,
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
    MutationResult,
    RunRequest,
    RunResponse,
    RunResult,
    StartSessionRequest,
    StartSessionResponse,
)
from daiv_sandbox.scripts import CMD_INIT_META_SCRIPT, CMD_TURN_DIFF_SCRIPT
from daiv_sandbox.sessions import (
    SANDBOX_ROOT,
    SKILLS_ROOT,
    WORKSPACE_ROOT,
    SandboxDockerSession,
    _validate_sandbox_path,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

HEADER_API_KEY_NAME = "X-API-Key"

DAIV_SANDBOX_TYPE_LABEL = "daiv.sandbox.type"
DAIV_SANDBOX_PATCH_EXTRACTOR_SESSION_ID_LABEL = "daiv.sandbox.patch_extractor_session_id"
DAIV_SANDBOX_WORKDIR_VOLUME_LABEL = "daiv.sandbox.workdir_volume"
DAIV_SANDBOX_MANAGED_LABEL = "daiv.sandbox.managed"

TYPE_PATCH_EXTRACTOR = "patch_extractor"
TYPE_CMD_EXECUTOR = "cmd_executor"

NO_CHANGES_MESSAGE = "nothing to commit, working tree clean"
EXIT_CODE_TIMEOUT = 124  # matches timeout(1) convention


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

    try:
        yield
    finally:
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
}


@app.post("/session/", responses=common_responses, name="Obtain a session ID")
async def start_session(request: StartSessionRequest, api_key: str = Depends(get_api_key)) -> StartSessionResponse:
    """
    Start a session and return the session ID.

    A Docker container is created with the base image provided.
    The session ID is used to identify the created container in subsequent requests.

    This session ID ensures a consistent execution environment for the commands, including files and directories.
    """
    import uuid

    cmd_executor_labels = {DAIV_SANDBOX_TYPE_LABEL: TYPE_CMD_EXECUTOR}
    workdir_volume_name: str | None = None

    if request.extract_patch:
        # Create a shared Docker volume for the workspace
        workdir_volume_name = f"daiv-sandbox-workdir-{uuid.uuid4()}"
        await asyncio.to_thread(
            SandboxDockerSession.create_named_volume, name=workdir_volume_name, labels={DAIV_SANDBOX_MANAGED_LABEL: "1"}
        )

        patch_extractor = await asyncio.to_thread(
            SandboxDockerSession.start,
            image=settings.GIT_IMAGE,
            labels={DAIV_SANDBOX_TYPE_LABEL: TYPE_PATCH_EXTRACTOR},
            network_mode="none",
            volumes={workdir_volume_name: {"bind": "/workdir/new", "mode": "ro"}},
        )

        cmd_executor_labels[DAIV_SANDBOX_PATCH_EXTRACTOR_SESSION_ID_LABEL] = patch_extractor.session_id
        cmd_executor_labels[DAIV_SANDBOX_WORKDIR_VOLUME_LABEL] = workdir_volume_name

    cmd_executor_kwargs = {}

    if workdir_volume_name:
        cmd_executor_kwargs["volumes"] = {workdir_volume_name: {"bind": SANDBOX_ROOT, "mode": "rw"}}

    if not request.network_enabled:
        cmd_executor_kwargs["network_mode"] = "none"

    if request.environment:
        cmd_executor_kwargs["environment"] = request.environment

    if request.memory_bytes:
        cmd_executor_kwargs["mem_limit"] = request.memory_bytes

    if request.cpus:
        cmd_executor_kwargs["cpus"] = request.cpus

    try:
        cmd_executor = await asyncio.to_thread(
            SandboxDockerSession.start, image=request.base_image, labels=cmd_executor_labels, **cmd_executor_kwargs
        )
    except Exception:
        # Clean up already-created resources on failure to avoid leaked containers/volumes.
        if workdir_volume_name:
            patch_extractor_sid = cmd_executor_labels.get(DAIV_SANDBOX_PATCH_EXTRACTOR_SESSION_ID_LABEL)
            if patch_extractor_sid:
                SandboxDockerSession(session_id=patch_extractor_sid).remove_container()
            try:
                SandboxDockerSession._get_shared_client().volumes.get(workdir_volume_name).remove(force=True)
            except Exception:
                logger.warning("Failed to clean up volume '%s' after session creation failure", workdir_volume_name)
        raise
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
                              extracted into ``/workspace/repo``. When present, the patch-extractor's
                              meta repo is initialised.
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
            cmd_executor.container.exec_run, ["/bin/sh", "-c", "test -f /workdir/.daiv-seeded"], user="root"
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

        # Meta init whenever a patch-extractor is linked (extract_patch=True at session start).
        # Even with no repo_archive (repoless runs or skills-only seeds) the agent will mutate
        # /workspace/repo via apply_file_mutations / bash; both endpoints run HEAD-advance and require
        # /workdir/meta to exist. The init script's seed commit is `--allow-empty`, so an empty
        # /workdir/new is fine.
        if extract_patch_session_id := cmd_executor.get_label(DAIV_SANDBOX_PATCH_EXTRACTOR_SESSION_ID_LABEL):
            patch_extractor = await asyncio.to_thread(SandboxDockerSession, session_id=extract_patch_session_id)
            init_result = await asyncio.to_thread(
                patch_extractor.execute_command, CMD_INIT_META_SCRIPT, workdir="/workdir"
            )
            if init_result.exit_code != 0:
                logger.error("Failed to init meta repo: [%s] %s", init_result.exit_code, init_result.output)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to initialise patch-extractor meta repo. Check logs.",
                )

        marker_result = await asyncio.to_thread(
            cmd_executor.container.exec_run, ["/bin/sh", "-c", "touch /workdir/.daiv-seeded"], user="root"
        )
        if marker_result.exit_code != 0:
            logger.error("Failed to mark session as seeded: [%s] %s", marker_result.exit_code, marker_result.output)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to mark session as seeded. Check logs.",
            )

        return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/session/{session_id}/files/", responses=common_responses, name="Apply file mutations to a session")
async def apply_file_mutations(
    http_request: Request,
    session_id: Annotated[str, FastAPIPath(title="The ID of the session to mutate.")],
    request: ApplyMutationsRequest,
    api_key: str = Depends(get_api_key),
) -> ApplyMutationsResponse:
    """
    Apply a batch of file mutations to /workspace/repo and advance the patch-extractor's meta HEAD.

    Per-item validation: each mutation that fails returns a MutationResult(ok=False, error=...).
    Request-level errors (4xx) are reserved for auth, schema, body-size, and unknown-session.
    """
    async with http_request.app.state.session_lock_manager.acquire(session_id):
        cmd_executor = await asyncio.to_thread(SandboxDockerSession, session_id=session_id)

        if not cmd_executor.container:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found or already closed")

        results: list[MutationResult] = []
        any_succeeded = False

        for mutation in request.mutations:
            try:
                _validate_sandbox_path(mutation.path, allowed_roots=(SANDBOX_ROOT,))
            except ValueError as exc:
                results.append(MutationResult(path=mutation.path, ok=False, error=str(exc)))
                continue

            try:
                await asyncio.to_thread(cmd_executor.write_file, mutation.path, mutation.content, mode=mutation.mode)
            except Exception as exc:
                logger.exception("apply_mutations: write failed for %s", mutation.path)
                results.append(MutationResult(path=mutation.path, ok=False, error=str(exc)))
                continue

            results.append(MutationResult(path=mutation.path, ok=True, error=None))
            any_succeeded = True

        if any_succeeded and (
            extract_patch_session_id := cmd_executor.get_label(DAIV_SANDBOX_PATCH_EXTRACTOR_SESSION_ID_LABEL)
        ):
            patch_extractor = await asyncio.to_thread(SandboxDockerSession, session_id=extract_patch_session_id)
            advance = await asyncio.to_thread(patch_extractor.execute_command, CMD_TURN_DIFF_SCRIPT, workdir="/workdir")
            if advance.exit_code != 0:
                logger.error("HEAD-advance failed: [%s] %s", advance.exit_code, advance.output)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Patch-extractor HEAD-advance failed; session may be inconsistent.",
                )

        return ApplyMutationsResponse(results=results)


@app.post("/session/{session_id}/", responses=common_responses, name="Run commands on a session")
async def run_on_session(
    http_request: Request,
    session_id: Annotated[str, FastAPIPath(title="The ID of the session to run commands in.")],
    request: RunRequest,
    api_key: str = Depends(get_api_key),
) -> RunResponse:
    """
    Run a set of commands on a session and return the results, including the patch of changes
    made by these commands (HEAD~1..HEAD against the meta repo).
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

        base64_patch: str | None = None

        if extract_patch_session_id := cmd_executor.get_label(DAIV_SANDBOX_PATCH_EXTRACTOR_SESSION_ID_LABEL):
            patch_extractor = await asyncio.to_thread(SandboxDockerSession, session_id=extract_patch_session_id)
            patch_result = await asyncio.to_thread(
                patch_extractor.execute_command, CMD_TURN_DIFF_SCRIPT, workdir="/workdir"
            )

            if patch_result.exit_code != 0 and NO_CHANGES_MESSAGE not in patch_result.output:
                logger.error("Failed to extract turn diff: [%s] %s", patch_result.exit_code, patch_result.output)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to extract patch with the changes made by the commands. Check logs.",
                )

            if NO_CHANGES_MESSAGE in patch_result.output or patch_result.output.strip() == "":
                base64_patch = None
            else:
                base64_patch = base64.b64encode(patch_result.output.encode()).decode()

        return RunResponse(results=results, patch=base64_patch)


@app.delete("/session/{session_id}/", responses=common_responses, name="Close a session")
async def close_session(
    request: Request,
    session_id: Annotated[str, FastAPIPath(title="The ID of the session to close")],
    api_key: str = Depends(get_api_key),
) -> Response:
    """
    Close a session by removing the Docker container and associated resources.
    """
    from docker.errors import NotFound as VolumeNotFound

    async with request.app.state.session_lock_manager.acquire(session_id):
        cmd_executor = await asyncio.to_thread(SandboxDockerSession, session_id=session_id)

        if not cmd_executor.container:
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        if patch_extractor_session_id := cmd_executor.get_label(DAIV_SANDBOX_PATCH_EXTRACTOR_SESSION_ID_LABEL):
            patch_extractor = await asyncio.to_thread(SandboxDockerSession, session_id=patch_extractor_session_id)
            await asyncio.gather(
                asyncio.to_thread(patch_extractor.remove_container), asyncio.to_thread(cmd_executor.remove_container)
            )
        else:
            await asyncio.to_thread(cmd_executor.remove_container)

        # Remove shared workdir volume after all containers are removed.
        if workdir_volume_name := cmd_executor.get_label(DAIV_SANDBOX_WORKDIR_VOLUME_LABEL):
            try:
                volume = await asyncio.to_thread(cmd_executor.client.volumes.get, workdir_volume_name)
                await asyncio.to_thread(volume.remove, force=False)
                logger.info("Removed shared volume '%s'", workdir_volume_name)
            except VolumeNotFound:
                logger.warning("Volume '%s' not found (already removed)", workdir_volume_name)
            except Exception as e:
                # Volume might still be in use or other error - log but don't fail the request
                logger.warning("Failed to remove volume '%s': %s", workdir_volume_name, e)

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
# The endpoints never invoke the patch-extractor themselves, but edits under repo/ land
# on the shared workdir volume the patch-extractor diffs, so those DO surface in the next
# patch; skills/ and tmp/ are container-local and stay diff-invisible.

_WORKSPACE_ROOTS = (WORKSPACE_ROOT,)


def _validate_workspace_dir(path: str) -> None:
    """Validate a directory/file path for ls/grep/glob: the workspace root itself or anything under it.

    Thin HTTP wrapper over ``_validate_sandbox_path(..., allow_root=True)``: unlike the file ops
    (which forbid the bare root), ls/grep/glob legitimately target ``/workspace`` itself. The shared
    validator still rejects ``..`` traversal, NUL, and newlines — otherwise the kernel would resolve
    ``/workspace/../etc`` and the search would escape WORKSPACE_ROOT.
    """
    try:
        _validate_sandbox_path(path, allowed_roots=_WORKSPACE_ROOTS, allow_root=True)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a glob (supporting **, *, ?, [..]) to a compiled regex anchored to a relative path.

    This engine is path-aware (``*`` does not cross ``/``, ``**`` does). It is intentionally NOT the
    same as the optional ``glob`` filter on ``fs_grep``, which uses ``fnmatch`` for a basename-only
    match — the two have deliberately different semantics, so don't try to merge them.
    """
    i, n, out = 0, len(pattern), []
    while i < n:
        c = pattern[i]
        if pattern[i : i + 3] == "**/":
            out.append("(?:.*/)?")
            i += 3
        elif pattern[i : i + 2] == "**":
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            negate = j < n and pattern[j] in "!^"
            if negate:
                j += 1
            if j < n and pattern[j] == "]":  # a leading ']' is a literal class member, not the closer
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            body = pattern[i + 1 + (1 if negate else 0) : j]
            out.append(f"[{'^' if negate else ''}{body}]")
            i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


@asynccontextmanager
async def _workspace_executor(http_request: Request, session_id: str) -> AsyncIterator[SandboxDockerSession]:
    """Acquire the session lock and yield a live cmd_executor, or 404."""
    async with http_request.app.state.session_lock_manager.acquire(session_id):
        cmd = await asyncio.to_thread(SandboxDockerSession, session_id=session_id)
        if not cmd.container:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found or already closed")
        yield cmd


@app.post("/session/{session_id}/fs/write", responses=common_responses, name="Write a workspace file")
async def fs_write(
    http_request: Request, session_id: str, request: FsWriteRequest, api_key: str = Depends(get_api_key)
) -> FsWriteResponse:
    try:
        _validate_sandbox_path(request.path, allowed_roots=_WORKSPACE_ROOTS)
    except ValueError as exc:
        return FsWriteResponse(ok=False, error=str(exc))
    async with _workspace_executor(http_request, session_id) as cmd:
        try:
            await asyncio.to_thread(
                cmd.write_file, request.path, request.content, mode=request.mode, allowed_roots=_WORKSPACE_ROOTS
            )
        except Exception as exc:
            logger.exception("fs_write failed for %s", request.path)
            return FsWriteResponse(ok=False, error=str(exc))
        return FsWriteResponse(ok=True)


@app.post("/session/{session_id}/fs/read", responses=common_responses, name="Read a workspace file")
async def fs_read(
    http_request: Request, session_id: str, request: FsReadRequest, api_key: str = Depends(get_api_key)
) -> FsReadResponse:
    try:
        _validate_sandbox_path(request.path, allowed_roots=_WORKSPACE_ROOTS)
    except ValueError as exc:
        return FsReadResponse(error=str(exc))
    async with _workspace_executor(http_request, session_id) as cmd:
        try:
            raw = await asyncio.to_thread(cmd.read_file_bytes, request.path)
        except FileNotFoundError:
            return FsReadResponse(error="file_not_found")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return FsReadResponse(content=base64.b64encode(raw).decode("ascii"), encoding="base64")
        if not text:
            return FsReadResponse(content="System reminder: File exists but has empty contents", encoding="utf-8")
        lines = text.splitlines()
        page = lines[request.offset : request.offset + request.limit]
        if request.offset and not page:
            return FsReadResponse(error=f"Line offset {request.offset} exceeds file length ({len(lines)} lines)")
        return FsReadResponse(content="\n".join(page), encoding="utf-8")


@app.post("/session/{session_id}/fs/ls", responses=common_responses, name="List a workspace directory")
async def fs_ls(
    http_request: Request, session_id: str, request: FsLsRequest, api_key: str = Depends(get_api_key)
) -> FsLsResponse:
    _validate_workspace_dir(request.path)
    async with _workspace_executor(http_request, session_id) as cmd:
        try:
            entries = await asyncio.to_thread(cmd.list_dir, request.path)
        except Exception as exc:
            logger.exception("fs_ls failed for %s", request.path)
            return FsLsResponse(error=str(exc))
        return FsLsResponse(entries=[FsEntry(path=p, is_dir=d) for p, d in entries])


@app.post("/session/{session_id}/fs/grep", responses=common_responses, name="Grep workspace files")
async def fs_grep(
    http_request: Request, session_id: str, request: FsGrepRequest, api_key: str = Depends(get_api_key)
) -> FsGrepResponse:
    _validate_workspace_dir(request.path)
    async with _workspace_executor(http_request, session_id) as cmd:
        try:
            matches = await asyncio.to_thread(cmd.grep, request.pattern, request.path, request.glob)
        except Exception as exc:
            logger.exception("fs_grep failed for %s", request.path)
            return FsGrepResponse(error=str(exc))
        return FsGrepResponse(matches=[FsGrepMatch(path=p, line=n, text=t) for p, n, t in matches])


@app.post("/session/{session_id}/fs/glob", responses=common_responses, name="Glob workspace files")
async def fs_glob(
    http_request: Request, session_id: str, request: FsGlobRequest, api_key: str = Depends(get_api_key)
) -> FsGlobResponse:
    _validate_workspace_dir(request.path)
    # Translate the glob up front so a malformed pattern is a clean 400, not a 500.
    try:
        regex = _glob_to_regex(request.pattern)
    except re.error as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"invalid glob pattern: {exc}") from exc
    async with _workspace_executor(http_request, session_id) as cmd:
        try:
            all_entries = await asyncio.to_thread(cmd.find_paths, request.path)
        except Exception as exc:
            logger.exception("fs_glob failed for %s", request.path)
            return FsGlobResponse(error=str(exc))
        base = request.path.rstrip("/")
        # Match the base-relative portion of each absolute entry under base.
        matched = [
            FsEntry(path=p, is_dir=d)
            for (p, d) in all_entries
            if p.startswith(f"{base}/") and regex.match(p[len(base) + 1 :])
        ]
        return FsGlobResponse(matches=matched)


@app.post("/session/{session_id}/fs/edit", responses=common_responses, name="Edit a workspace file")
async def fs_edit(
    http_request: Request, session_id: str, request: FsEditRequest, api_key: str = Depends(get_api_key)
) -> FsEditResponse:
    try:
        _validate_sandbox_path(request.path, allowed_roots=_WORKSPACE_ROOTS)
    except ValueError as exc:
        return FsEditResponse(error=str(exc))
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
        except FileNotFoundError:
            return FsEditResponse(error="file_not_found")
        except UnicodeDecodeError:
            return FsEditResponse(error="not_a_text_file")
        except ValueError as exc:
            return FsEditResponse(error=str(exc))
        return FsEditResponse(occurrences=count)


@app.post("/session/{session_id}/fs/delete", responses=common_responses, name="Delete a workspace file")
async def fs_delete(
    http_request: Request, session_id: str, request: FsDeleteRequest, api_key: str = Depends(get_api_key)
) -> FsDeleteResponse:
    try:
        _validate_sandbox_path(request.path, allowed_roots=_WORKSPACE_ROOTS)
    except ValueError as exc:
        return FsDeleteResponse(ok=False, error=str(exc))
    async with _workspace_executor(http_request, session_id) as cmd:
        try:
            await asyncio.to_thread(cmd.delete_file, request.path)
        except Exception as exc:
            logger.exception("fs_delete failed for %s", request.path)
            return FsDeleteResponse(ok=False, error=str(exc))
        return FsDeleteResponse(ok=True)


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
