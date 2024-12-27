import signal
from unittest.mock import MagicMock, patch

import pytest
from docker.errors import ImageNotFound
from docker.models.containers import ExecResult
from docker.models.images import Image

from daiv_sandbox.config import settings
from daiv_sandbox.sessions import PRIVILEGED_USER, SandboxDockerSession, handler


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
            )
        )
        mock_from_env.return_value = mock_client

        yield mock_client


@patch("daiv_sandbox.sessions.signal.alarm")
def test_context_manager(mock_signal_alarm, mock_docker_client, mock_image):
    with SandboxDockerSession(image="test-image") as session:
        assert session.container is not None
        mock_signal_alarm.assert_called_once_with(settings.MAX_EXECUTION_TIME)
    mock_signal_alarm.assert_called_with(0)  # Ensure alarm is reset


@patch("daiv_sandbox.sessions.signal.alarm", side_effect=[TimeoutError, None])
def test_context_manager_timeout(mock_signal_alarm, mock_docker_client, mock_image):
    with pytest.raises(RuntimeError, match="Execution timed out"):  # noqa: SIM117
        with SandboxDockerSession(image="test-image"):
            pass
    mock_signal_alarm.assert_called_with(0)  # Ensure alarm is reset


def test_open_with_image(mock_docker_client, mock_image):
    session = SandboxDockerSession(image="test-image", run_id="test-run-id")
    session.open()
    mock_docker_client.images.get.assert_called_once_with("test-image")
    mock_docker_client.containers.run.assert_called_once_with(
        mock_image, detach=True, tty=True, runtime="runc", hostname="sandbox", name="sandbox-test-run-id"
    )
    assert session.image == mock_image
    assert session.container is not None


def test_open_with_image_not_found_pulls_image(mock_docker_client):
    mock_docker_client.images.get.side_effect = ImageNotFound("test-image")
    session = SandboxDockerSession(image="test-image")
    session.open()
    mock_docker_client.images.pull.assert_called_once_with("test-image")
    assert session.image is not None
    assert session.container is not None


def test_open_with_invalid_image_type_raises_error():
    with pytest.raises(ValueError, match="Invalid image type"):
        session = SandboxDockerSession(image=MagicMock())
        session.open()


def test_open_with_dockerfile(mock_docker_client):
    session = SandboxDockerSession(dockerfile="/home/user/Dockerfile")
    session.open()
    mock_docker_client.images.build.assert_called_once_with(
        path="/home/user", dockerfile="Dockerfile", tag="sandbox-user"
    )


def test_open_with_both_image_and_dockerfile_raises_error():
    with pytest.raises(ValueError, match="Only one of image or dockerfile should be provided"):
        SandboxDockerSession(image="test-image", dockerfile="/home/user/Dockerfile")


def test_open_without_image_or_dockerfile_raises_error():
    with pytest.raises(ValueError, match="Either image or dockerfile should be provided"):
        SandboxDockerSession()


def test_close_removes_container():
    container = MagicMock()
    session = SandboxDockerSession(image="test-image")
    session.container = container
    session.close()
    container.remove.assert_called_once_with(force=True)


def test_execute_command():
    session = SandboxDockerSession(image="test-image")
    session.container = MagicMock()
    session.container.exec_run.return_value = ExecResult(exit_code=0, output=b"output")
    result = session.execute_command("echo hello", "/")
    assert result.exit_code == 0
    assert result.output == "output"


def test_copy_to_runtime_creates_directory():
    session = SandboxDockerSession(image="test-image")
    session.container = MagicMock()
    session.container.exec_run.side_effect = [ExecResult(exit_code=1, output=b""), ExecResult(exit_code=0, output=b"")]
    with patch("io.BytesIO", return_value=MagicMock()) as mock_data:
        session.copy_to_runtime("/path/to/dest", mock_data)
        session.container.exec_run.assert_any_call("mkdir -p /path/to/dest", privileged=True, user=PRIVILEGED_USER)


def test_copy_from_runtime_raises_error_if_file_not_found():
    session = SandboxDockerSession(image="test-image")
    session.container = MagicMock()
    session.container.get_archive.return_value = ([], {"size": 0})
    with pytest.raises(FileNotFoundError):
        session.copy_from_runtime("/path/to/src")


@patch("daiv_sandbox.sessions.from_env")
def test_ping_successful(mock_from_env):
    mock_client = MagicMock()
    mock_client.ping.return_value = True
    mock_from_env.return_value = mock_client

    assert SandboxDockerSession.ping() is True
    mock_client.ping.assert_called_once()


@patch("daiv_sandbox.sessions.from_env")
def test_ping_unsuccessful(mock_from_env):
    mock_client = MagicMock()
    mock_client.ping.return_value = False
    mock_from_env.return_value = mock_client

    assert SandboxDockerSession.ping() is False
    mock_client.ping.assert_called_once()


def test_handler_raises_timeout_error():
    """Test that the handler function raises TimeoutError when called"""
    with pytest.raises(TimeoutError, match="Execution timed out"):
        handler(signal.SIGALRM, None)


def test_handler_is_registered_for_sigalrm():
    """Test that the handler is properly registered for SIGALRM"""
    current_handler = signal.getsignal(signal.SIGALRM)
    assert current_handler == handler, "Handler should be registered for SIGALRM"
