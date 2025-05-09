import datetime
import io
import logging
import shlex
import signal
import tarfile
from abc import ABC, abstractmethod
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Literal
from uuid import uuid4

from docker import DockerClient, from_env
from docker.errors import ImageNotFound
from docker.models.images import Image

from daiv_sandbox.config import settings
from daiv_sandbox.schemas import RunResult

if TYPE_CHECKING:
    from docker.models.containers import Container

logger = logging.getLogger("daiv_sandbox")


def handler(signum, frame):
    raise TimeoutError("Execution timed out")


signal.signal(signal.SIGALRM, handler)


class Session(ABC):
    @abstractmethod
    def open(self):
        raise NotImplementedError

    @abstractmethod
    def close(self):
        raise NotImplementedError

    @abstractmethod
    def copy_from_runtime(self, src: str) -> BinaryIO:
        raise NotImplementedError

    @abstractmethod
    def copy_to_runtime(self, data: BinaryIO):
        raise NotImplementedError

    @abstractmethod
    def execute_command(self, command: str, workdir: str, extract_changed_files: bool = False) -> RunResult:
        raise NotImplementedError

    def __enter__(self):
        try:
            signal.alarm(settings.MAX_EXECUTION_TIME)

            self.open()
            return self
        except TimeoutError as e:
            self.__exit__()
            raise RuntimeError("Execution timed out") from e
        except Exception as e:
            self.__exit__()
            raise e

    def __exit__(self, *args, **kwargs):
        signal.alarm(0)
        self.close()


class SandboxDockerSession(Session):
    def __init__(
        self,
        client: DockerClient | None = None,
        image: str | None = None,
        dockerfile: str | None = None,
        keep_template: bool = False,
        runtime: Literal["runc", "runsc"] = "runc",
        run_id: str | None = None,
    ):
        """
        Create a new sandbox session using Docker.

        Args:
            client: Docker client, if not provided, a new client will be created based on local Docker context
            image: Docker image to use
            dockerfile: Path to the Dockerfile, if image is not provided
            keep_template: if True, the image and container will not be removed after the session ends
            runtime: the container runtime to use, either "runc" or "runsc"
            run_id: the run ID to use for the container
        """
        if image and dockerfile:
            raise ValueError("Only one of image or dockerfile should be provided")

        elif not image and not dockerfile:
            raise ValueError("Either image or dockerfile should be provided")

        if not client:
            logger.info("Using local Docker context since client is not provided.")

        self.client: DockerClient = client or from_env()
        self.image: Image | str | None = image
        self.dockerfile = Path(dockerfile) if dockerfile else None
        self.container: Container | None = None
        self.keep_template = keep_template
        self.is_create_template: bool = False
        self.runtime = runtime
        self.run_id = run_id or str(uuid4())

    @staticmethod
    def ping() -> bool:
        """
        Ping the Docker client.
        """
        client = from_env()
        return client.ping()

    def open(self):
        """
        Create a new container from the image.
        """
        if self.dockerfile:
            logger.info("Building docker image from %s", self.dockerfile)

            path = self.dockerfile.parent
            self.image, _ = self.client.images.build(
                path=str(path), dockerfile=self.dockerfile.name, tag=f"sandbox-{path.name}"
            )
            self.is_create_template = True

        elif isinstance(self.image, str):
            try:
                self.image = self.client.images.get(self.image)
                logger.info("Found already existing image %s", self.image.tags[-1])
            except ImageNotFound:
                logger.info("Pulling image %s", self.image)
                self.image = self.client.images.pull(self.image)
                self.is_create_template = True
        else:
            raise ValueError("Invalid image type")

        self.container = self.client.containers.run(
            self.image,
            command='sh -c "tail -f /dev/null"',  # Keep container running indefinitely
            detach=True,
            tty=True,
            runtime=self.runtime,
            hostname="sandbox",
            name=f"sandbox-{self.run_id}",
        )
        logger.info("Container %s created", self.container.short_id)

    @property
    def run_path(self) -> str:
        """
        Get the path to the run directory.
        """
        if self._image_user == "root":
            return f"/runs/{self.run_id}"

        return (Path(self._image_working_dir) / "runs" / self.run_id).as_posix()

    @property
    def _image_user(self) -> str:
        # If it's empty, Docker defaults to root
        return self._image_inspection["Config"]["User"] or "root"

    @property
    def _image_working_dir(self) -> str:
        if not self._image_inspection["Config"]["WorkingDir"]:
            raise ValueError("Can't determine the working dir of image %s", self._image_inspection["RepoTags"][0])

        return self._image_inspection["Config"]["WorkingDir"]

    @cached_property
    def _image_inspection(self) -> dict:
        if not self.container or not self.image:
            raise RuntimeError("Session is not open. Please call open() method before copying files.")

        return self.client.api.inspect_image(self.image.tags[-1])

    def close(self):
        """
        Remove the container.
        """
        if self.container:
            self.container.remove(force=True)
            self.container = None

        if self.is_create_template and not self.keep_template:
            containers: list[Container] = self.client.containers.list(all=True)
            image: Image = self.image if isinstance(self.image, Image) else self.client.images.get(self.image)

            if any(container.image.id == image.id for container in containers):
                logger.warning("Image %s is in use by other containers. Skipping removal.", image.tags[-1])
            else:
                image.remove(force=True)

    def _ensure_container_running(self):
        """
        Check if the container exists and is running. If not, attempt to restart it.
        """
        if not self.container:
            raise RuntimeError("Session is not open. Please call open() method before executing commands.")

        try:
            # Refresh container status
            self.container.reload()

            # Check if container is running
            if self.container.status != "running":
                logger.warning(
                    "Container %s is not running (status: %s). Attempting to restart...",
                    self.container.short_id,
                    self.container.status,
                )
                self.container.restart()
                self.container.reload()

        except Exception as e:
            raise RuntimeError(f"Failed to ensure container {self.container.short_id} is running: {str(e)}") from e
        else:
            if self.container.status != "running":
                raise RuntimeError(
                    f"Failed to restart container {self.container.short_id}. Current status: {self.container.status}"
                )

            logger.info("Container %s successfully restarted", self.container.short_id)

    def copy_from_runtime(self, src: str) -> BinaryIO:
        """
        Copy a file or directory from the container to the host.
        """
        self._ensure_container_running()

        logger.info("Copying archive from %s:%s...", self.container.short_id, src)

        bits, stat = self.container.get_archive(src)
        if stat["size"] == 0:
            raise FileNotFoundError(f"File {src} not found in the container {self.container.short_id}")

        tarstream = io.BytesIO()
        for chunk in bits:
            tarstream.write(chunk)
        tarstream.seek(0)

        extracted_archive = io.BytesIO()
        with tarfile.open(fileobj=tarstream, mode="r:*") as tar:
            extracted_archive.write(tar.extractfile(stat["name"]).read())
        extracted_archive.seek(0)

        return extracted_archive

    def copy_to_runtime(self, data: BinaryIO):
        """
        Copy a file or directory to the container.
        """
        self._ensure_container_running()

        if self.container.exec_run(f"test -d {self.run_path}")[0] != 0:
            logger.info("Creating directory %s:%s...", self.container.short_id, self.run_path)
            result = self.container.exec_run(f"mkdir -p {self.run_path}")
            if result.exit_code != 0:
                raise RuntimeError(
                    f"Failed to create directory {self.container.short_id}:{self.run_path} "
                    f"(exit code {result.exit_code}) -> {result.output.decode()}"
                )

        logger.info("Copying archive to %s:%s...", self.container.short_id, self.run_path)

        if self.container.put_archive(self.run_path, data.getvalue()):
            # we need to normalizer folder permissions
            logger.debug("Normalizing folder permissions for %s:%s", self.container.short_id, self.run_path)
            self.container.exec_run(
                f"chown -R {self._image_user}:{self._image_user} {self.run_path}", privileged=True, user="root"
            )
            logger.debug("Successfully copied archive to %s:%s", self.container.short_id, self.run_path)
        else:
            raise RuntimeError(f"Failed to copy archive to {self.container.short_id}:{self.run_path}")

    def execute_command(
        self, command: str, workdir: str | None = None, extract_changed_files: bool = False
    ) -> RunResult:
        """
        Execute a command in the container.
        """
        self._ensure_container_running()

        command_workdir = (Path(self.run_path) / workdir).as_posix() if workdir else self.run_path

        logger.info("Executing command '%s' in %s:%s...", command, self.container.short_id, command_workdir)

        before_run_date = datetime.datetime.now()

        result = self.container.exec_run(f"/bin/sh -c {shlex.quote(command)}", workdir=command_workdir)

        if result.exit_code != 0:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "Command '%s' in %s:%s exited with code %s: %s",
                    command,
                    self.container.short_id,
                    command_workdir,
                    result.exit_code,
                    result.output.decode(),
                )
            else:
                logger.warning(
                    "Command '%s' in %s:%s exited with code %s",
                    command,
                    self.container.short_id,
                    command_workdir,
                    result.exit_code,
                )

        return RunResult(
            command=command,
            output=result.output.decode(),
            exit_code=result.exit_code,
            workdir=command_workdir,
            changed_files=self._extract_changed_file_names(command_workdir, before_run_date)
            if extract_changed_files
            else [],
        )

    def _extract_changed_file_names(self, workdir: str, modified_after: datetime.datetime) -> list[str]:
        """
        Extract the list of changed files in the container.
        """
        if not self.container:
            logger.info("Session already closed. Skipping extraction of changed files.")
            return []

        self._ensure_container_running()

        logger.info("Extracting list of changed files from %s:%s...", self.container.short_id, workdir)

        # Get the list of changed files in the specified workdir and modified after the specified date.
        result = self.container.exec_run(
            f'find {workdir} -type f ! -path "*/.*" '
            f'-newermt "{modified_after:%Y-%m-%d %H:%M:%S}.{modified_after.microsecond // 1000:03d}"'
        )

        if result.exit_code != 0:
            raise RuntimeError(
                f"Failed to get the list of changed files from {self.container.short_id}:{workdir} "
                f"(exit code {result.exit_code}) -> {result.output.decode()}"
            )

        changed_files = result.output.decode().splitlines()

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Extracted changed files: %s", changed_files)

        if not changed_files:
            logger.info("No changed files found in %s:%s", self.container.short_id, workdir)
            return []

        return [file.replace(f"{workdir}/", "") for file in changed_files]

    def create_tar_gz_archive(self, workdir: str, include_files: list[str]) -> BinaryIO:
        """
        Create a tar.gz archive with the specified files.
        """
        self._ensure_container_running()

        logger.info(
            "Creating tar.gz file with %s files in %s:%s...", len(include_files), self.container.short_id, workdir
        )

        tar_path = f"{workdir}/changed_files.tar.gz"
        result = self.container.exec_run(f"tar -czf {tar_path} -C {workdir} {' '.join(include_files)}")

        if result.exit_code != 0:
            logger.debug(
                "Failed to create tar.gz file with changed files in %s:%s (exit code %s) -> %s",
                self.container.short_id,
                workdir,
                result.exit_code,
                result.output.decode(),
            )
            raise RuntimeError(
                f"Failed to create tar.gz file with changed files in {self.container.short_id}:{workdir} "
                f"(exit code {result.exit_code}) -> {result.output.decode()}"
            )

        return self.copy_from_runtime(tar_path)
