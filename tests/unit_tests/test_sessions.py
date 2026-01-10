from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from docker.errors import ImageNotFound, NotFound
from docker.models.containers import ExecResult
from docker.models.images import Image

from daiv_sandbox.config import settings
from daiv_sandbox.sessions import SANDBOX_ROOT, SandboxDockerSession


@pytest.fixture
def mock_image():
    return Image({"id": "test-image", "RepoTags": ["test-image"]})


@pytest.fixture
def mock_docker_client(mock_image):
    with patch("daiv_sandbox.sessions.from_env") as mock_from_env:
        mock_client = MagicMock(
            images=MagicMock(
                build=MagicMock(return_value=(mock_image, None)),
                get=MagicMock(return_value=mock_image),
                pull=MagicMock(return_value=mock_image),
                ping=MagicMock(return_value=True),
            )
        )
        mock_from_env.return_value = mock_client

        yield mock_client


def test_ping(mock_docker_client):
    with patch.object(SandboxDockerSession, "_ping", return_value=True) as mock_ping:
        assert SandboxDockerSession.ping() is True
        mock_ping.assert_called_once()


def test_start_with_image(mock_docker_client, mock_image):
    with (
        patch.object(SandboxDockerSession, "_pull_image") as mock_pull_image,
        patch.object(SandboxDockerSession, "_start_container") as mock_start_container,
    ):
        SandboxDockerSession.start(image="test-image")
        mock_pull_image.assert_called_once_with("test-image")
        mock_start_container.assert_called_once_with("test-image")


@patch("daiv_sandbox.sessions.tempfile.NamedTemporaryFile")
def test_start_with_dockerfile(mock_named_temporary_file, mock_docker_client):
    mock_named_temporary_file.return_value.__enter__.return_value.name = "test-dockerfile"
    with (
        patch.object(SandboxDockerSession, "_build_image") as mock_build_image,
        patch.object(SandboxDockerSession, "_start_container") as mock_start_container,
    ):
        SandboxDockerSession.start(dockerfile="/home/user/Dockerfile")
        mock_build_image.assert_called_once_with(Path("test-dockerfile"))
        mock_start_container.assert_called_once_with(mock_build_image.return_value)


def test__pull_image_with_image_not_found(mock_docker_client):
    mock_docker_client.images.get.side_effect = ImageNotFound("test-image")
    session = SandboxDockerSession()
    session._pull_image("test-image")
    mock_docker_client.images.pull.assert_called_once_with("test-image")


def test__pull_image_with_image_found(mock_docker_client):
    session = SandboxDockerSession()
    session._pull_image("test-image")
    mock_docker_client.images.get.assert_called_once_with("test-image")


def test__build_image(mock_docker_client):
    session = SandboxDockerSession()
    dockerfile = MagicMock(name="test-dockerfile")
    result = session._build_image(dockerfile)
    mock_docker_client.images.build.assert_called_once_with(
        path=dockerfile.parent.as_posix(), dockerfile=dockerfile.name, tag=f"sandbox-{dockerfile.name}"
    )
    assert result == mock_docker_client.images.build.return_value[0].tags[-1]


def test__start_container(mock_docker_client):
    session = SandboxDockerSession()
    mock_container = mock_docker_client.containers.run.return_value
    mock_container.exec_run.return_value = ExecResult(exit_code=0, output=b"")
    session._start_container("test-image")
    mock_docker_client.containers.run.assert_called_once_with(
        "test-image",
        entrypoint="/bin/sh",
        command=["-lc", "sleep 3600"],
        detach=True,
        tty=True,
        runtime=settings.RUNTIME,
        remove=True,
    )
    assert session.container is not None
    assert session.container.id == mock_docker_client.containers.run.return_value.id
    assert session.session_id == mock_docker_client.containers.run.return_value.id
    # Should create SANDBOX_ROOT directory
    mock_container.exec_run.assert_called_once_with(["mkdir", "-p", "--", SANDBOX_ROOT], user="root")


def test_remove_container(mock_docker_client):
    session = SandboxDockerSession(session_id="test-session-id")
    session.remove_container()
    mock_docker_client.containers.get.assert_called_with(session.session_id)
    mock_docker_client.containers.get.return_value.remove.assert_called_once_with(force=True)


def test_remove_container_with_container_not_found(mock_docker_client):
    session = SandboxDockerSession(session_id="test-session-id")
    mock_docker_client.containers.get.side_effect = NotFound(session.session_id)
    session.remove_container()
    mock_docker_client.containers.get.assert_called_with(session.session_id)
    mock_docker_client.containers.get.return_value.remove.assert_not_called()


def test_copy_to_runtime_creates_directory(mock_docker_client):
    session = SandboxDockerSession()
    session.container = MagicMock()
    session.container.exec_run.side_effect = [
        ExecResult(exit_code=0, output=b""),  # rm
        ExecResult(exit_code=0, output=b""),  # mkdir
        ExecResult(exit_code=0, output=b""),  # chmod
    ]
    with patch("io.BytesIO", return_value=MagicMock()) as mock_data:
        session.copy_to_container(mock_data)
        # Should use SANDBOX_ROOT by default
        session.container.exec_run.assert_any_call(["rm", "-rf", "--", f"{SANDBOX_ROOT}/*"], user="root")
        session.container.exec_run.assert_any_call(["mkdir", "-p", "--", SANDBOX_ROOT], user="root")
        session.container.exec_run.assert_any_call(["chmod", "-R", "a+rX,u+w", "--", SANDBOX_ROOT], user="root")


def test_copy_from_runtime_raises_error_if_file_not_found():
    session = SandboxDockerSession()
    session.container = MagicMock()
    session.container.get_archive.return_value = ([], {"size": 0})
    with pytest.raises(FileNotFoundError):
        session.copy_from_container("/path/to/src")
    # Absolute paths should be used as-is
    session.container.get_archive.assert_called_once_with("/path/to/src")


def test_execute_command(mock_docker_client):
    session = SandboxDockerSession(session_id="test-session-id")
    session.container = MagicMock()
    session.container.exec_run.return_value = ExecResult(exit_code=0, output=b"output")
    result = session.execute_command("echo hello")
    assert result.exit_code == 0
    assert result.output == "output"
    # Should use SANDBOX_ROOT by default
    session.container.exec_run.assert_called_once_with(["/bin/sh", "-c", "echo hello"], workdir=SANDBOX_ROOT)


def test_copy_to_container_with_relative_dest(mock_docker_client):
    """Test that relative dest paths are resolved under SANDBOX_ROOT"""
    session = SandboxDockerSession()
    session.container = MagicMock()
    session.container.exec_run.side_effect = [
        ExecResult(exit_code=0, output=b""),  # rm
        ExecResult(exit_code=0, output=b""),  # mkdir
        ExecResult(exit_code=0, output=b""),  # chmod
    ]
    with patch("io.BytesIO", return_value=MagicMock()) as mock_data:
        session.copy_to_container(mock_data, dest="subdir")
        expected_path = f"{SANDBOX_ROOT}/subdir"
        session.container.exec_run.assert_any_call(["rm", "-rf", "--", f"{expected_path}/*"], user="root")
        session.container.exec_run.assert_any_call(["mkdir", "-p", "--", expected_path], user="root")
        session.container.exec_run.assert_any_call(["chmod", "-R", "a+rX,u+w", "--", expected_path], user="root")


def test_copy_from_container_with_relative_path(mock_docker_client):
    """Test that relative paths are resolved under SANDBOX_ROOT"""
    session = SandboxDockerSession()
    session.container = MagicMock()
    session.container.get_archive.return_value = ([b"data"], {"size": 100})
    session.copy_from_container("subdir")
    expected_path = f"{SANDBOX_ROOT}/subdir"
    session.container.get_archive.assert_called_once_with(expected_path)


def test_execute_command_with_relative_workdir(mock_docker_client):
    """Test that relative workdir is resolved under SANDBOX_ROOT"""
    session = SandboxDockerSession(session_id="test-session-id")
    session.container = MagicMock()
    session.container.exec_run.return_value = ExecResult(exit_code=0, output=b"output")
    result = session.execute_command("echo hello", workdir="subdir")
    assert result.exit_code == 0
    expected_workdir = f"{SANDBOX_ROOT}/subdir"
    session.container.exec_run.assert_called_once_with(["/bin/sh", "-c", "echo hello"], workdir=expected_workdir)


def test_execute_command_with_absolute_workdir(mock_docker_client):
    """Test that absolute workdir is used as-is"""
    session = SandboxDockerSession(session_id="test-session-id")
    session.container = MagicMock()
    session.container.exec_run.return_value = ExecResult(exit_code=0, output=b"output")
    result = session.execute_command("echo hello", workdir="/custom/path")
    assert result.exit_code == 0
    session.container.exec_run.assert_called_once_with(["/bin/sh", "-c", "echo hello"], workdir="/custom/path")
