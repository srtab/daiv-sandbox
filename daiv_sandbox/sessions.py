from __future__ import annotations

import io
import logging
import tarfile
from abc import ABC, abstractmethod
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, BinaryIO

from docker import DockerClient, from_env
from docker.errors import ImageNotFound, NotFound

from daiv_sandbox.config import settings
from daiv_sandbox.schemas import RunResult

if TYPE_CHECKING:
    from docker.models.containers import Container
    from docker.models.volumes import Volume

logger = logging.getLogger("daiv_sandbox")

# Canonical sandbox root directory inside all containers
SANDBOX_ROOT = "/repo"
WORKDIR_ROOT = "/workdir"
SANDBOX_HOME = "/home/daiv-sandbox"


def _sh_quote(value: str) -> str:
    """
    Safely quote an arbitrary string for POSIX shell usage.

    This is intentionally tiny (avoid importing shlex just for one call site).
    """
    # POSIX-safe single-quote escaping:  abc'd -> 'abc'"'"'d'
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _normalize_tar_member_name(name: str) -> str | None:
    """
    Normalize tar member names and reject traversal / absolute paths.

    Returns:
        Normalized POSIX path (no leading "./"), or None for empty/root entries.
    """
    # Strip leading "./" segments (common in tar archives).
    while name.startswith("./"):
        name = name[2:]

    if name in {"", "."}:
        return None

    p = PurePosixPath(name)
    if p.is_absolute():
        raise ValueError(f"Archive contains an absolute path: {name!r}")
    if ".." in p.parts:
        raise ValueError(f"Archive contains a parent-directory traversal path: {name!r}")

    normalized = str(p)
    return None if normalized in {"", "."} else normalized


def _sanitize_archive_bytes(data: bytes, *, uid: int, gid: int) -> bytes:
    """
    Sanitize an incoming (possibly compressed) tar archive for safer extraction.

    - Rejects symlinks, hardlinks, device nodes, and FIFOs.
    - Rejects absolute paths and '..' traversal.
    - Normalizes ownership to the sandbox uid/gid.
    - Normalizes permissions similar to: chmod -R a+rX,u+w

    Returns an *uncompressed* tar archive.
    """
    out_buf = io.BytesIO()
    try:
        with (
            tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as in_tf,
            tarfile.open(fileobj=out_buf, mode="w") as out_tf,
        ):
            for member in in_tf:
                normalized_name = _normalize_tar_member_name(member.name)
                if normalized_name is None:
                    continue

                if not (member.isfile() or member.isdir()):
                    raise ValueError(
                        "Archive contains an unsupported entry type "
                        f"({member.name!r}, type={member.type!r}); only files/dirs are allowed"
                    )

                # Mirror `chmod -R a+rX,u+w` semantics while clearing special bits.
                base_mode = member.mode & 0o777
                base_mode &= ~0o7000  # clear suid/sgid/sticky

                mode = base_mode | 0o444 | 0o200  # a+r, u+w
                if member.isdir() or (base_mode & 0o111):  # a+X
                    mode |= 0o111
                else:
                    mode &= ~0o111

                out_info = tarfile.TarInfo(name=normalized_name)
                out_info.uid = uid
                out_info.gid = gid
                out_info.uname = ""
                out_info.gname = ""
                out_info.mtime = 0
                out_info.mode = mode

                if member.isdir():
                    out_info.type = tarfile.DIRTYPE
                    out_info.size = 0
                    out_tf.addfile(out_info)
                    continue

                # Regular file
                extracted = in_tf.extractfile(member)
                if extracted is None:
                    raise ValueError(f"Failed to read file entry from archive: {member.name!r}")

                try:
                    out_info.type = tarfile.REGTYPE
                    out_info.size = member.size
                    out_tf.addfile(out_info, fileobj=extracted)
                finally:
                    extracted.close()
    except tarfile.TarError as e:  # pragma: no cover (depends on tarfile internals)
        raise ValueError("Invalid tar archive") from e

    return out_buf.getvalue()


class Session(ABC):
    @classmethod
    @abstractmethod
    def ping(cls, client: DockerClient | None = None) -> bool:
        """
        Ping the Docker client.
        """

    @classmethod
    @abstractmethod
    def start(cls, client: DockerClient | None = None):
        """
        Start a new session.
        """

    @abstractmethod
    def copy_from_container(self, host_dir: str) -> BinaryIO:
        """
        Copy a file or directory from the container to the host.
        """

    @abstractmethod
    def copy_to_container(self, data: BinaryIO, dest: str | None = None):
        """
        Copy a file or directory to the container.
        """

    @abstractmethod
    def execute_command(self, command: str, workdir: str | None = None) -> RunResult:
        """
        Execute a command in the container.
        """


class SandboxDockerSession(Session):
    """
    A session is a Docker container that is used to execute commands.
    """

    def __init__(self, session_id: str | None = None, client: DockerClient | None = None):
        """
        Create a new sandbox session using Docker.

        Args:
            client: Docker client, if not provided, a new client will be created based on local Docker context
        """
        self.session_id: str | None = session_id
        self.client: DockerClient = client or from_env()
        self.container: Container | None = self._get_container(session_id) if session_id else None

    @classmethod
    def ping(cls, *, client: DockerClient | None = None) -> bool:
        """
        Ping the Docker client.

        Args:
            client (DockerClient | None): Docker client, if not provided, a new client will be created based on
                local Docker context.

        Returns:
            bool: True if the client is pingable, False otherwise.
        """
        return cls(client=client)._ping()

    @classmethod
    def start(cls, image: str, *, client: DockerClient | None = None, **kwargs) -> SandboxDockerSession:
        """
        Start a new session by pulling the image and creating a new container.

        Args:
            image (str): Docker image to use.
            client (DockerClient | None): Docker client, if not provided, a new client will be created based on
                local Docker context.

        Returns:
            SandboxDockerSession: The session object.
        """
        instance = cls(client=client)

        instance._pull_image(image)
        instance._start_container(image, **kwargs)
        return instance

    @classmethod
    def create_named_volume(
        cls, name: str, labels: dict[str, str] | None = None, client: DockerClient | None = None
    ) -> Volume:
        """
        Create a named volume with the given name and labels.

        Args:
            name (str): The name of the volume to create.
            labels (dict[str, str] | None): The labels to add to the volume.
            client (DockerClient | None): Docker client, if not provided, a new client will be created based on
                local Docker context.

        Returns:
            Volume: The created volume.
        """
        instance = cls(client=client)
        return instance.client.volumes.create(name=name, labels=labels or {})

    def _ping(self) -> bool:
        """
        Ping the Docker client.
        """
        return self.client.ping()

    def _pull_image(self, image: str):
        """
        Pull the image from the registry.

        Args:
            image (str): The tag of the image to pull.
        """
        try:
            found_image = self.client.images.get(image)
            logger.info("Found already existing image '%s'", found_image.tags[-1])
        except ImageNotFound:
            logger.info("Pulling image '%s'", image)
            self.client.images.pull(image)

    def _start_container(self, image: str, **kwargs):
        """
        Create a new container from the image.

        Args:
            image (str): The tag of the image to use.
        """
        if "user" in kwargs:
            raise ValueError("Sandbox containers always run as a non-root user; overriding `user` is not allowed.")

        container = self.client.containers.run(
            image,
            entrypoint="/bin/sh",
            command=["-lc", "sleep 3600"],  # 1 hour
            detach=True,
            tty=True,
            runtime=settings.RUNTIME,
            remove=True,
            user=self._get_user(),
            **kwargs,
        )

        self.session_id = container.id
        self.container = container

        logger.info("Container '%s' created (status: %s)", container.short_id, container.status)

        # Ensure the sandbox directories exist and are writable by the sandbox user.
        mkdir_result = container.exec_run(["mkdir", "-p", "--", SANDBOX_ROOT, WORKDIR_ROOT, SANDBOX_HOME], user="root")
        if mkdir_result.exit_code != 0:
            raise RuntimeError(
                f"Failed to create sandbox directories in {container.short_id}: "
                f"(exit_code: {mkdir_result.exit_code}) -> {mkdir_result.output}"
            )

        chown_result = container.exec_run(
            ["chown", self._get_user(), "--", SANDBOX_ROOT, WORKDIR_ROOT, SANDBOX_HOME], user="root"
        )
        if chown_result.exit_code != 0:
            raise RuntimeError(
                f"Failed to chown sandbox directories in {container.short_id}: "
                f"(exit_code: {chown_result.exit_code}) -> {chown_result.output}"
            )

    def remove_container(self):
        """
        Remove the container.

        Args:
            session_id (str): The ID of the container to remove.
        """
        try:
            self.client.containers.get(self.session_id).remove(force=True)
        except NotFound:
            logger.warning("Container '%s' not found", self.session_id)
        else:
            logger.info("Container '%s' removed", self.session_id)

    def copy_from_container(self, host_dir: str) -> BinaryIO:
        """
        Copy a file or directory from the container to the host.

        Args:
            session_id (str): The ID of the container to copy the archive from.
            host_dir (str): The path to the file or directory to copy from the container.

        Returns:
            BinaryIO: The copied archive.
        """
        if Path(host_dir).is_absolute():
            from_dir = host_dir
        elif host_dir in {"", "."}:
            # Special case: when copying the sandbox root itself ("."), request SANDBOX_ROOT + "/." so the archive
            # contains the *contents* rather than a top-level "sandbox-root/" directory.
            from_dir = f"{SANDBOX_ROOT}/."
        else:
            from_dir = (Path(SANDBOX_ROOT) / host_dir).as_posix()

        bits, stat = self.container.get_archive(from_dir)

        if stat["size"] == 0:
            raise FileNotFoundError(f"File {from_dir} not found in the container {self.container.short_id}")

        return io.BytesIO(b"".join(bits))

    def copy_to_container(self, tardata: BinaryIO, dest: str | None = None, clear_before_copy: bool = True):
        """
        Copy a file or directory to a specific path in the container.

        Args:
            session_id (str): The ID of the container to copy the archive to.
            tardata (BinaryIO): The tar archive to be copied to the container.
            dest (str | None): The destination path to copy the archive to. Defaults to SANDBOX_ROOT.
            clear_before_copy (bool): Whether to clear the destination directory before copying the archive.
        """
        # Read and sanitize archive bytes before sending to the Docker daemon.
        raw_bytes: bytes
        if hasattr(tardata, "getvalue"):
            raw_bytes = tardata.getvalue()  # type: ignore[no-any-return]
        else:
            if hasattr(tardata, "seek"):
                tardata.seek(0)
            raw_bytes = tardata.read()

        sanitized = _sanitize_archive_bytes(raw_bytes, uid=settings.RUN_UID, gid=settings.RUN_GID)

        # Resolve destination: default to SANDBOX_ROOT, absolute stays absolute, relative resolves under SANDBOX_ROOT
        to_dir = SANDBOX_ROOT
        if dest:
            to_dir = dest if Path(dest).is_absolute() else (Path(SANDBOX_ROOT) / dest).as_posix()

        to_dir_norm = to_dir.rstrip("/") or "/"
        if to_dir_norm in {"", "/"}:
            raise ValueError("Refusing to extract an archive into the container root directory")

        if not (
            to_dir_norm in (SANDBOX_ROOT, WORKDIR_ROOT)
            or to_dir_norm.startswith(f"{SANDBOX_ROOT}/")
            or to_dir_norm.startswith(f"{WORKDIR_ROOT}/")
        ):
            raise ValueError(f"Refusing to extract an archive outside of {SANDBOX_ROOT!r} or {WORKDIR_ROOT!r}")

        if clear_before_copy:
            q = _sh_quote(to_dir_norm)
            rm_cmd = f"rm -rf -- {q}/* {q}/.[!.]* {q}/..?* 2>/dev/null || true"
            rm_result = self.container.exec_run(["/bin/sh", "-c", rm_cmd], user="root")
            if rm_result.exit_code != 0:
                raise RuntimeError(
                    f"Failed to clear directory {self.container.short_id}:{to_dir_norm}: "
                    f"(exit_code: {rm_result.exit_code}) -> {rm_result.output}"
                )

        mkdir_result = self.container.exec_run(["mkdir", "-p", "--", to_dir_norm], user="root")
        if mkdir_result.exit_code != 0:
            raise RuntimeError(
                f"Failed to create directory {self.container.short_id}:{to_dir_norm}: "
                f"(exit_code: {mkdir_result.exit_code}) -> {mkdir_result.output}"
            )

        if self.container.put_archive(to_dir_norm, sanitized):
            # Normalize permissions/ownership on the extracted tree.
            #
            # NOTE: The archive is sanitized to disallow symlinks/hardlinks, which avoids dangerous recursive
            # chmod/chown dereferencing behavior on attacker-controlled link targets.
            chmod_result = self.container.exec_run(["chmod", "-R", "a+rX,u+w", "--", to_dir_norm], user="root")
            chown_result = self.container.exec_run(["chown", "-R", self._get_user(), "--", to_dir_norm], user="root")
            if chmod_result.exit_code != 0:
                raise RuntimeError(
                    f"Failed to normalize permissions of {self.container.short_id}:{to_dir_norm}: "
                    f"(exit_code: {chmod_result.exit_code}) -> {chmod_result.output}"
                )
            if chown_result.exit_code != 0:
                raise RuntimeError(
                    f"Failed to normalize ownership for {self.container.short_id}:{to_dir_norm}: "
                    f"(exit_code: {chown_result.exit_code}) -> {chown_result.output}"
                )
        else:
            raise RuntimeError(f"Failed to copy archive to {self.container.short_id}:{to_dir_norm}")

    def execute_command(self, command: str, workdir: str | None = None) -> RunResult:
        """
        Execute a command in the container.

        Args:
            session_id (str): The ID of the container to execute the command in.
            command (str): The command to execute.
            workdir (str | None): The working directory of the command. Defaults to SANDBOX_ROOT.

        Returns:
            RunResult: The result of the command.
        """
        # Resolve workdir: default to SANDBOX_ROOT, absolute stays absolute, relative resolves under SANDBOX_ROOT
        command_workdir = SANDBOX_ROOT
        if workdir:
            command_workdir = workdir if Path(workdir).is_absolute() else (Path(SANDBOX_ROOT) / workdir).as_posix()

        logger.info("Executing command in %s:%s -> '%s'", self.container.short_id, command_workdir, command)

        result = self.container.exec_run(
            ["/bin/sh", "-c", command],
            workdir=command_workdir,
            user=self._get_user(),
            environment=self._get_exec_environment(),
        )

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Command in %s:%s exited with code %s: %s",
                self.container.short_id,
                command_workdir,
                result.exit_code,
                result.output.decode(),
            )
        elif result.exit_code != 0:
            logger.warning(
                "Command in %s:%s exited with code %s", self.container.short_id, command_workdir, result.exit_code
            )

        return RunResult(
            command=command, output=result.output.decode(), exit_code=result.exit_code, workdir=command_workdir
        )

    def _get_user(self) -> str:
        """
        Get the user to execute sandbox commands as.
        """
        return f"{settings.RUN_UID}:{settings.RUN_GID}"

    def _get_exec_environment(self) -> dict[str, str]:
        """
        Provide a writable HOME/XDG environment for sandboxed commands.

        This avoids failures when HOME is unset, set to '/', or non-writable.
        """
        home = SANDBOX_HOME
        return {
            "HOME": home,
            "XDG_CACHE_HOME": f"{home}/.cache",
            "XDG_CONFIG_HOME": f"{home}/.config",
            "XDG_STATE_HOME": f"{home}/.local/state",
            "XDG_DATA_HOME": f"{home}/.local/share",
        }

    def _get_container(self, session_id: str) -> Container | None:
        """
        Get the container by ID. If the container is not running, attempt to restart it.

        Args:
            session_id (str): The ID of the container to ensure is running.

        Returns:
            Container | None: The container object if it exists and is running, None otherwise.
        """
        try:
            container = self.client.containers.get(session_id)
        except NotFound:
            return None

        try:
            container.reload()

            if container.status != "running":
                logger.warning(
                    "Container '%s' is not running (status: %s). Attempting to restart...",
                    container.short_id,
                    container.status,
                )
                container.restart()
                container.reload()

        except Exception:
            logger.exception("Failed to ensure container %s is running", container.short_id)
            return None
        else:
            if container.status != "running":
                logger.error("Failed to restart container %s. Current status: %s", container.short_id, container.status)
                return None

        return container

    def get_label(self, label: str) -> str | None:
        """
        Get a label from the container.

        Args:
            label (str): The label to get.

        Returns:
            str | None: The value of the label, or None if the label is not set.
        """
        return self.container.attrs["Config"].get("Labels", {}).get(label)
