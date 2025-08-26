from __future__ import annotations

import datetime
import io
import logging
import shlex
import signal
import tarfile
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from docker import DockerClient, from_env
from docker.errors import ImageNotFound, NotFound

from daiv_sandbox.config import settings
from daiv_sandbox.schemas import ImageInspection, RunResult

if TYPE_CHECKING:
    from docker.models.containers import Container

logger = logging.getLogger("daiv_sandbox")


def handler(signum, frame):
    raise TimeoutError("Execution timed out")


signal.signal(signal.SIGALRM, handler)


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

    @classmethod
    @abstractmethod
    def end(cls, client: DockerClient | None = None):
        """
        End the session.
        """

    @abstractmethod
    def copy_from_container(self, session_id: str, src: str) -> BinaryIO:
        """
        Copy a file or directory from the container to the host.
        """

    @abstractmethod
    def copy_to_container(self, session_id: str, data: BinaryIO):
        """
        Copy a file or directory to the container.
        """

    @abstractmethod
    def execute_command(
        self, session_id: str, command: str, workdir: str, extract_changed_files: bool = False
    ) -> RunResult:
        """
        Execute a command in the container.
        """


class SandboxDockerSession(Session):
    """
    A session is a Docker container that is used to execute commands.
    """

    def __init__(self, client: DockerClient | None = None):
        """
        Create a new sandbox session using Docker.

        Args:
            client: Docker client, if not provided, a new client will be created based on local Docker context
        """
        self.client: DockerClient = client or from_env()

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
        cls, image: str | None = None, dockerfile: str | None = None, *, client: DockerClient | None = None
    ) -> str:
        """
        Start a new session by building or pulling the image and creating a new container.

        Args:
            image (str | None): Docker image to use, if not provided, the image will be built from the Dockerfile.
            dockerfile (str | None): Dockerfile content to build the image from.
            client (DockerClient | None): Docker client, if not provided, a new client will be created based on
                local Docker context.

        Returns:
            str: The ID of the created container.
        """
        assert dockerfile or image, "Either image or dockerfile should be provided"

        instance = cls(client)

        if image:
            instance._pull_image(image)

        elif dockerfile:
            with tempfile.NamedTemporaryFile() as f:
                f.write(dockerfile.encode())
                f.flush()
                image = instance._build_image(Path(f.name))

        return instance._start_container(image)

    @classmethod
    def end(cls, session_id: str, *, client: DockerClient | None = None):
        """
        End the session by removing the container.

        Args:
            session_id (str): The ID of the container to remove.
            client (DockerClient | None): Docker client, if not provided, a new client will be created based on
                local Docker context.
        """
        instance = cls(client)
        instance._remove_container(session_id)

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

    def _start_container(self, image: str) -> str:
        """
        Create a new container from the image.

        Args:
            image (str): The tag of the image to use.

        Returns:
            str: The ID of the created container.
        """
        container = self.client.containers.run(
            image,
            command='sh -c "tail -f /dev/null"',  # Keep container running indefinitely
            detach=True,
            tty=True,
            runtime=settings.RUNTIME,
            hostname="sandbox",
        )
        logger.info("Container '%s' created", container.short_id)
        return container.id

    def _remove_container(self, session_id: str):
        """
        Remove the container.

        Args:
            session_id (str): The ID of the container to remove.
        """
        try:
            self.client.containers.get(session_id).remove(force=True)
        except NotFound:
            logger.warning("Container '%s' not found", session_id)
        else:
            logger.info("Container '%s' removed", session_id)

    def copy_from_container(self, container: Container, src: str) -> BinaryIO:
        """
        Copy a file or directory from the container to the host.

        Args:
            session_id (str): The ID of the container to copy the archive from.
            src (str): The path to the file or directory to copy from the container.

        Returns:
            BinaryIO: The copied archive.
        """
        logger.info("Copying archive from %s:%s...", container.short_id, src)

        bits, stat = container.get_archive(src)
        if stat["size"] == 0:
            raise FileNotFoundError(f"File {src} not found in the container {container.short_id}")

        tarstream = io.BytesIO()
        for chunk in bits:
            tarstream.write(chunk)
        tarstream.seek(0)

        extracted_archive = io.BytesIO()
        with tarfile.open(fileobj=tarstream, mode="r:*") as tar:
            extracted_archive.write(tar.extractfile(stat["name"]).read())
        extracted_archive.seek(0)

        return extracted_archive

    def copy_to_container(self, container: Container, data: BinaryIO):
        """
        Copy a file or directory to the container.

        Args:
            session_id (str): The ID of the container to copy the archive to.
            data (BinaryIO): The archive to copy to the container.

        """
        image_inspection = self._get_image_inspection(container.image.tags[-1])

        if container.exec_run(f"test -d {image_inspection.working_dir}")[0] != 0:
            logger.info("Creating directory %s:%s...", container.short_id, image_inspection.working_dir)
            result = container.exec_run(f"mkdir -p {image_inspection.working_dir}")
            if result.exit_code != 0:
                raise RuntimeError(
                    f"Failed to create directory {container.short_id}:{image_inspection.working_dir} "
                    f"(exit code {result.exit_code}) -> {result.output.decode()}"
                )

        logger.info("Copying archive to %s:%s...", container.short_id, image_inspection.working_dir)

        if container.put_archive(image_inspection.working_dir, data.getvalue()):
            # we need to normalizer folder permissions
            logger.debug("Normalizing folder permissions for %s:%s", container.short_id, image_inspection.working_dir)
            container.exec_run(
                f"chown -R {image_inspection.user}:{image_inspection.user} {image_inspection.working_dir}",
                privileged=True,
                user="root",
            )
            logger.debug("Successfully copied archive to %s:%s", container.short_id, image_inspection.working_dir)
        else:
            raise RuntimeError(f"Failed to copy archive to {container.short_id}:{image_inspection.working_dir}")

    def execute_command(
        self, container: Container, command: str, workdir: str | None = None, extract_changed_files: bool = False
    ) -> RunResult:
        """
        Execute a command in the container.

        Args:
            session_id (str): The ID of the container to execute the command in.
            command (str): The command to execute.
            workdir (str | None): The working directory of the command.
            extract_changed_files (bool): Whether to extract the changed files.

        Returns:
            RunResult: The result of the command.
        """
        image_inspection = self._get_image_inspection(container.image.tags[-1])

        command_workdir = (
            (Path(image_inspection.working_dir) / workdir).as_posix() if workdir else image_inspection.working_dir
        )

        logger.info("Executing command '%s' in %s:%s...", command, container.short_id, command_workdir)

        before_run_date = datetime.datetime.now()

        result = container.exec_run(f"/bin/sh -c {shlex.quote(command)}", workdir=command_workdir)

        if result.exit_code != 0:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "Command '%s' in %s:%s exited with code %s: %s",
                    command,
                    container.short_id,
                    command_workdir,
                    result.exit_code,
                    result.output.decode(),
                )
            else:
                logger.warning(
                    "Command '%s' in %s:%s exited with code %s",
                    command,
                    container.short_id,
                    command_workdir,
                    result.exit_code,
                )

        return RunResult(
            command=command,
            output=result.output.decode(),
            exit_code=result.exit_code,
            workdir=command_workdir,
            changed_files=self._extract_changed_file_names(container, command_workdir, before_run_date)
            if extract_changed_files
            else [],
        )

    def _extract_changed_file_names(
        self, container: Container, workdir: str, modified_after: datetime.datetime
    ) -> list[str]:
        """
        Extract the list of changed files in the container.

        Args:
            container (Container): The container to extract the changed files from.
            workdir (str): The working directory of the container to extract the changed files from.
            modified_after (datetime.datetime): The date after which the files were modified.

        Returns:
            list[str]: The list of changed files.
        """
        logger.info("Extracting list of changed files from %s:%s...", container.short_id, workdir)

        # Get the list of changed files in the specified workdir and modified after the specified date.
        result = container.exec_run(
            f'find {workdir} -type f ! -path "*/.*" '
            f'-newermt "{modified_after:%Y-%m-%d %H:%M:%S}.{modified_after.microsecond // 1000:03d}"'
        )

        if result.exit_code != 0:
            raise RuntimeError(
                f"Failed to get the list of changed files from {container.short_id}:{workdir} "
                f"(exit code {result.exit_code}) -> {result.output.decode()}"
            )

        changed_files = result.output.decode().splitlines()

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Extracted changed files: %s", changed_files)

        if not changed_files:
            logger.info("No changed files found in %s:%s", container.short_id, workdir)
            return []

        return [file.replace(f"{workdir}/", "") for file in changed_files]

    def create_tar_gz_archive(self, container: Container, workdir: str, include_files: list[str]) -> BinaryIO:
        """
        Create a tar.gz archive with the specified files.

        Args:
            session_id (str): The ID of the container to create the archive from.
            workdir (str): The working directory of the container to create the archive from.
            include_files (list[str]): The list of files to include in the archive.

        Returns:
            BinaryIO: The tar.gz archive.
        """
        logger.info("Creating tar.gz file with %s files in %s:%s...", len(include_files), container.short_id, workdir)

        tar_path = f"{workdir}/changed_files.tar.gz"
        result = container.exec_run(f"tar -czf {tar_path} -C {workdir} {' '.join(include_files)}")

        if result.exit_code != 0:
            logger.debug(
                "Failed to create tar.gz file with changed files in %s:%s (exit code %s) -> %s",
                container.short_id,
                workdir,
                result.exit_code,
                result.output.decode(),
            )
            raise RuntimeError(
                f"Failed to create tar.gz file with changed files in {container.short_id}:{workdir} "
                f"(exit code {result.exit_code}) -> {result.output.decode()}"
            )

        return self.copy_from_container(container, tar_path)

    def get_container(self, session_id: str) -> Container | None:
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

    def _get_image_inspection(self, image: str) -> ImageInspection:
        """
        Get the inspection of the image.

        Args:
            image (str): The tag of the image to inspect.

        Returns:
            ImageInspection: The inspection of the image.
        """
        return ImageInspection.from_inspection(self.client.api.inspect_image(image))
