from __future__ import annotations

import io
import logging
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from docker import DockerClient, from_env
from docker.errors import ImageNotFound, NotFound

from daiv_sandbox.config import settings
from daiv_sandbox.schemas import ImageAttrs, RunResult

if TYPE_CHECKING:
    from docker.models.containers import Container

logger = logging.getLogger("daiv_sandbox")


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
        self.image_attrs: ImageAttrs | None = (
            self._inspect_image(self.container.image.tags[-1]) if self.container else None
        )

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
        return cls(client)._ping()

    @classmethod
    def start(
        cls, image: str | None = None, dockerfile: str | None = None, *, client: DockerClient | None = None, **kwargs
    ) -> SandboxDockerSession:
        """
        Start a new session by building or pulling the image and creating a new container.

        Args:
            image (str | None): Docker image to use, if not provided, the image will be built from the Dockerfile.
            dockerfile (str | None): Dockerfile content to build the image from.
            client (DockerClient | None): Docker client, if not provided, a new client will be created based on
                local Docker context.

        Returns:
            SandboxDockerSession: The session object.
        """
        assert dockerfile or image, "Either image or dockerfile should be provided"

        instance = cls(client=client)

        if image:
            instance._pull_image(image)

        elif dockerfile:
            with tempfile.NamedTemporaryFile() as f:
                f.write(dockerfile.encode())
                f.flush()
                image = instance._build_image(Path(f.name))

        instance._start_container(image, **kwargs)
        return instance

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

    def _build_image(self, dockerfile: Path) -> str:
        """
        Build the image from the Dockerfile.

        Args:
            dockerfile (Path): The path to the Dockerfile to build.

        Returns:
            str: The tag of the built image.
        """
        logger.info("Building docker image from '%s'", dockerfile.as_posix())
        image, _logs = self.client.images.build(
            path=dockerfile.parent.as_posix(), dockerfile=dockerfile.name, tag=f"sandbox-{dockerfile.name}"
        )

        return image.tags[-1]

    def _start_container(self, image: str, **kwargs):
        """
        Create a new container from the image.

        Args:
            image (str): The tag of the image to use.
        """
        container = self.client.containers.run(
            image,
            entrypoint="/bin/sh",
            command=["-lc", "sleep 600"],  # 10 minutes
            detach=True,
            tty=True,
            runtime=settings.RUNTIME,
            remove=True,
            **kwargs,
        )

        self.session_id = container.id
        self.container = container

        logger.info("Container '%s' created (status: %s)", container.short_id, container.status)

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
        from_dir = (
            host_dir if Path(host_dir).is_absolute() else (Path(self.image_attrs.working_dir) / host_dir).as_posix()
        )

        logger.info("Copying archive from %s:%s...", self.container.short_id, from_dir)

        bits, stat = self.container.get_archive(from_dir)

        if stat["size"] == 0:
            raise FileNotFoundError(f"File {from_dir} not found in the container {self.container.short_id}")

        return io.BytesIO(b"".join(bits))

    def copy_to_container(self, tardata: BinaryIO, dest: str | None = None):
        """
        Copy a file or directory to a specific path in the container.

        Args:
            session_id (str): The ID of the container to copy the archive to.
            tardata (BinaryIO): The tar archive to be copied to the container.
            dest (str | None): The destination path to copy the archive to. Defaults to the working directory of the
                image.
        """
        to_dir = self.image_attrs.working_dir

        if dest:
            to_dir = dest if Path(dest).is_absolute() else (Path(to_dir) / dest).as_posix()

        logger.info("Creating directory %s:%s...", self.container.short_id, to_dir)

        rm_result = self.container.exec_run(["rm", "-rf", "--", f"{to_dir}/*"])
        if rm_result.exit_code != 0:
            raise RuntimeError(
                f"Failed to remove directory {self.container.short_id}:{to_dir} "
                f"(exit code {rm_result.exit_code}) -> {rm_result.output}"
            )

        mkdir_result = self.container.exec_run(["mkdir", "-p", "--", to_dir])
        if mkdir_result.exit_code != 0:
            raise RuntimeError(
                f"Failed to create directory {self.container.short_id}:{to_dir} "
                f"(exit code {mkdir_result.exit_code}) -> {mkdir_result.output}"
            )

        logger.info("Copying archive to %s:%s...", self.container.short_id, to_dir)

        if self.container.put_archive(to_dir, tardata.getvalue()):
            user = f"{self.image_attrs.user}:{self.image_attrs.user}"

            # Normalize folder permissions.
            self.container.exec_run(["chown", "-R", user, "--", to_dir], privileged=True, user="root")
        else:
            raise RuntimeError(f"Failed to copy archive to {self.container.short_id}:{to_dir}")

    def execute_command(self, command: str, workdir: str | None = None) -> RunResult:
        """
        Execute a command in the container.

        Args:
            session_id (str): The ID of the container to execute the command in.
            command (str): The command to execute.
            workdir (str | None): The working directory of the command.

        Returns:
            RunResult: The result of the command.
        """
        command_workdir = self.image_attrs.working_dir

        if workdir:
            if Path(workdir).is_absolute():
                command_workdir = workdir
            else:
                command_workdir = (Path(self.image_attrs.working_dir) / workdir).as_posix()

        logger.info("Executing command in %s:%s -> %s ", self.container.short_id, command_workdir, command)

        result = self.container.exec_run(command, workdir=command_workdir)

        if result.exit_code != 0:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "Command in %s:%s exited with code %s: %s",
                    self.container.short_id,
                    command_workdir,
                    result.exit_code,
                    result.output.decode(),
                )
            else:
                logger.warning(
                    "Command in %s:%s exited with code %s", self.container.short_id, command_workdir, result.exit_code
                )

        return RunResult(
            command=command, output=result.output.decode(), exit_code=result.exit_code, workdir=command_workdir
        )

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

    def _inspect_image(self, image: str) -> ImageAttrs:
        """
        Inspect the image to get the user and working directory.

        Args:
            image (str): The tag of the image to inspect.

        Returns:
            ImageInspection: The inspection of the image.
        """
        return ImageAttrs.from_inspection(self.client.api.inspect_image(image))

    def get_label(self, label: str) -> str | None:
        """
        Get a label from the container.

        Args:
            label (str): The label to get.

        Returns:
            str | None: The value of the label, or None if the label is not set.
        """
        return self.container.attrs["Config"].get("Labels", {}).get(label)
