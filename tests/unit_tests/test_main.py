import base64
import io
import uuid
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from daiv_sandbox import __version__
from daiv_sandbox.config import settings
from daiv_sandbox.main import app
from daiv_sandbox.schemas import RunResult


@pytest.fixture
def mock_session():
    with patch("daiv_sandbox.main.SandboxDockerSession") as mock_session:
        mock_session = mock_session(session_id=str(uuid.uuid4()))
        mock_session._get_container.return_value = Mock(status="running")
        # By default, no patch extraction (no label set)
        mock_session.get_label.return_value = None
        yield mock_session


@pytest.fixture
def client():
    return TestClient(app, headers={"X-API-Key": settings.API_KEY.get_secret_value()}, root_path=settings.API_V1_STR)


@pytest.mark.parametrize("endpoint", ["/session/", "/session/id/"])
def test_missing_api_key(client, endpoint):
    # Send a request without the API key
    client.headers = {}
    response = client.post(endpoint, json={})
    assert response.status_code == 403
    assert response.json() == {"detail": "API Key header is missing"}


@pytest.mark.parametrize("endpoint", ["/session/", "/session/id/"])
def test_invalid_api_key(client, endpoint):
    # Send a request with an invalid API key
    client.headers["X-API-Key"] = "invalid_key"
    response = client.post(endpoint, json={})
    assert response.status_code == 403
    assert response.json() == {"detail": "Invalid API Key"}


def test_close_session_missing_api_key(mock_session, client):
    client.headers = {}
    response = client.delete(f"/session/{mock_session.session_id}/")
    assert response.status_code == 403
    assert response.json() == {"detail": "API Key header is missing"}


def test_close_session_invalid_api_key(mock_session, client):
    client.headers["X-API-Key"] = "invalid_key"
    response = client.delete(f"/session/{mock_session.session_id}/")
    assert response.status_code == 403
    assert response.json() == {"detail": "Invalid API Key"}


def test_run_commands_success(mock_session, client):  # noqa: N803
    # Mock the session and its methods
    mock_session.execute_command.return_value = RunResult(
        command="echo 'Hello, World!'", output=b"success", exit_code=0, changed_files=[], workdir="/"
    )

    # Create a request payload with a valid UUID4
    request_payload = {"commands": ["echo 'Hello, World!'"], "archive": base64.b64encode(b"test").decode()}

    # Send a POST request to the endpoint
    response = client.post(f"/session/{mock_session.session_id}/", json=request_payload)

    # Assert the response
    assert response.status_code == 200, response.text
    response_data = response.json()
    assert "results" in response_data
    assert "patch" in response_data
    assert response_data["results"][0]["output"] == "success"
    assert response_data["results"][0]["exit_code"] == 0


def test_run_commands_failure(mock_session, client):  # noqa: N803
    # Mock the session and its methods
    mock_session.execute_command.return_value = RunResult(
        command="exit 1", output=b"error", exit_code=1, changed_files=[], workdir="/"
    )
    # Use a valid Base64-encoded string
    mock_session.create_tar_gz_archive.return_value = io.BytesIO(b"mocked_archive")

    # Create a request payload with a valid UUID4
    request_payload = {
        "commands": ["exit 1"],
        "archive": base64.b64encode(b"test").decode(),  # Base64 for "test"
    }

    # Send a POST request to the endpoint
    response = client.post(f"/session/{mock_session.session_id}/", json=request_payload)

    # Assert the response
    assert response.status_code == 200, response.text
    response_data = response.json()
    assert "results" in response_data
    assert "patch" in response_data
    assert response_data["results"][0]["output"] == "error"
    assert response_data["results"][0]["exit_code"] == 1


def test_run_commands_with_workdir(mock_session, client):  # noqa: N803
    # Mock the session and its methods
    mock_session.execute_command.return_value = RunResult(
        command="echo 'Hello, World!'", output=b"success", exit_code=0, changed_files=[], workdir="/"
    )
    mock_session.create_tar_gz_archive.return_value = io.BytesIO(b"mocked_archive")

    # Create a request payload with a valid UUID4 and workdir
    request_payload = {
        "commands": ["echo 'Hello, World!'"],
        "archive": base64.b64encode(b"test").decode(),
        "workdir": "/app",
    }

    # Send a POST request to the endpoint
    response = client.post(f"/session/{mock_session.session_id}/", json=request_payload)

    # Assert the response
    assert response.status_code == 200, response.text
    response_data = response.json()
    assert "results" in response_data
    assert "patch" in response_data
    assert response_data["results"][0]["output"] == "success"
    assert response_data["results"][0]["exit_code"] == 0


def test_run_commands_multiple_success_fail_fast_false(mock_session, client):  # noqa: N803
    # Mock the session and its methods
    mock_session.execute_command.side_effect = [
        RunResult(command="echo 'first'", output=b"first", exit_code=0, changed_files=[], workdir="/"),
        RunResult(command="echo 'second'", output=b"second", exit_code=0, changed_files=[], workdir="/"),
    ]
    mock_session.create_tar_gz_archive.return_value = io.BytesIO(b"mocked_archive")

    request_payload = {
        "commands": ["echo 'first'", "echo 'second'"],
        "archive": base64.b64encode(b"test").decode(),
        "fail_fast": False,
    }

    response = client.post(f"/session/{mock_session.session_id}/", json=request_payload)

    assert response.status_code == 200
    response_data = response.json()
    assert len(response_data["results"]) == 2
    assert mock_session.execute_command.call_count == 2


def test_run_commands_multiple_success_fail_fast_true(mock_session, client):  # noqa: N803
    # Mock the session and its methods
    mock_session.execute_command.side_effect = [
        RunResult(command="echo 'first'", output=b"first", exit_code=0, changed_files=[], workdir="/"),
        RunResult(command="echo 'second'", output=b"second", exit_code=0, changed_files=[], workdir="/"),
    ]
    mock_session.create_tar_gz_archive.return_value = io.BytesIO(b"mocked_archive")

    request_payload = {
        "commands": ["echo 'first'", "echo 'second'"],
        "archive": base64.b64encode(b"test").decode(),
        "fail_fast": True,
    }

    response = client.post(f"/session/{mock_session.session_id}/", json=request_payload)

    assert response.status_code == 200
    response_data = response.json()
    assert len(response_data["results"]) == 2
    assert mock_session.execute_command.call_count == 2


def test_run_commands_fail_fast_stops_on_failure(mock_session, client):  # noqa: N803
    # Mock the session and its methods
    mock_session.execute_command.side_effect = [
        RunResult(command="echo 'first'", output=b"first", exit_code=0, changed_files=[], workdir="/"),
        RunResult(command="exit 1", output=b"error", exit_code=1, changed_files=[], workdir="/"),
    ]
    mock_session.create_tar_gz_archive.return_value = io.BytesIO(b"mocked_archive")

    request_payload = {
        "commands": ["echo 'first'", "exit 1", "echo 'third'"],
        "archive": base64.b64encode(b"test").decode(),
        "fail_fast": True,
    }

    response = client.post(f"/session/{mock_session.session_id}/", json=request_payload)

    assert response.status_code == 200
    response_data = response.json()
    assert len(response_data["results"]) == 2  # Only first two commands executed
    assert response_data["results"][0]["exit_code"] == 0
    assert response_data["results"][1]["exit_code"] == 1
    assert mock_session.execute_command.call_count == 2  # Third command not executed


def test_run_commands_fail_fast_false_continues_on_failure(mock_session, client):  # noqa: N803
    # Mock the session and its methods
    mock_session.execute_command.side_effect = [
        RunResult(command="echo 'first'", output=b"first", exit_code=0, changed_files=[], workdir="/"),
        RunResult(command="exit 1", output=b"error", exit_code=1, changed_files=[], workdir="/"),
        RunResult(command="echo 'third'", output=b"third", exit_code=0, changed_files=[], workdir="/"),
    ]
    mock_session.create_tar_gz_archive.return_value = io.BytesIO(b"mocked_archive")

    request_payload = {
        "commands": ["echo 'first'", "exit 1", "echo 'third'"],
        "archive": base64.b64encode(b"test").decode(),
        "fail_fast": False,
    }

    response = client.post(f"/session/{mock_session.session_id}/", json=request_payload)

    assert response.status_code == 200
    response_data = response.json()
    assert len(response_data["results"]) == 3  # All commands executed
    assert response_data["results"][0]["exit_code"] == 0
    assert response_data["results"][1]["exit_code"] == 1
    assert response_data["results"][2]["exit_code"] == 0
    assert mock_session.execute_command.call_count == 3  # All commands executed


def test_run_commands_single_command_fail_fast_true(mock_session, client):  # noqa: N803
    # Mock the session and its methods
    mock_session.execute_command.return_value = RunResult(
        command="echo 'single'", output=b"single", exit_code=0, changed_files=[], workdir="/"
    )
    mock_session.create_tar_gz_archive.return_value = io.BytesIO(b"mocked_archive")

    request_payload = {"commands": ["echo 'single'"], "archive": base64.b64encode(b"test").decode(), "fail_fast": True}

    response = client.post(f"/session/{mock_session.session_id}/", json=request_payload)

    assert response.status_code == 200
    response_data = response.json()
    assert len(response_data["results"]) == 1
    assert response_data["results"][0]["exit_code"] == 0
    assert mock_session.execute_command.call_count == 1


def test_health(client):
    response = client.get("/-/health/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_version(client):
    response = client.get("/-/version/")
    assert response.status_code == 200
    assert response.json() == {"version": __version__}


def test_start_session_with_extract_patch_creates_volume(client):
    """Test that starting a session with extract_patch=true creates a volume and mounts it correctly."""
    with patch("daiv_sandbox.main.SandboxDockerSession") as mock_session_class:
        mock_patch_extractor = Mock()
        mock_patch_extractor.session_id = "patch-extractor-id"

        mock_cmd_executor = Mock()
        mock_cmd_executor.session_id = "cmd-executor-id"
        mock_session_class.start.side_effect = [mock_patch_extractor, mock_cmd_executor]

        # Make the request
        response = client.post("/session/", json={"base_image": "python:3.11", "extract_patch": True})

        assert response.status_code == 200
        assert response.json() == {"session_id": "cmd-executor-id"}

        # Verify volume was created
        mock_session_class.create_named_volume.assert_called_once()
        call_kwargs = mock_session_class.create_named_volume.call_args[1]
        assert "daiv-sandbox-workdir-" in call_kwargs["name"]
        assert call_kwargs["labels"] == {"daiv.sandbox.managed": "1"}

        # Verify both containers were started with correct volume mounts
        assert mock_session_class.start.call_count == 2

        # First call should be patch extractor with ro mount
        patch_call = mock_session_class.start.call_args_list[0]
        assert "volumes" in patch_call[1]
        volume_name = list(patch_call[1]["volumes"].keys())[0]
        assert patch_call[1]["volumes"][volume_name] == {"bind": "/workdir/new", "mode": "ro"}

        # Second call should be cmd executor with rw mount
        cmd_call = mock_session_class.start.call_args_list[1]
        assert "volumes" in cmd_call[1]
        volume_name = list(cmd_call[1]["volumes"].keys())[0]
        assert cmd_call[1]["volumes"][volume_name] == {"bind": "/repo", "mode": "rw"}


def test_close_session_removes_volume(client):
    """Test that closing a session removes the associated volume."""
    from daiv_sandbox.main import DAIV_SANDBOX_PATCH_EXTRACTOR_SESSION_ID_LABEL, DAIV_SANDBOX_WORKDIR_VOLUME_LABEL

    with patch("daiv_sandbox.main.SandboxDockerSession") as mock_session_class:
        # Mock the cmd executor
        mock_cmd_executor = Mock()
        mock_cmd_executor.session_id = "cmd-executor-id"
        mock_cmd_executor.container = Mock()

        # Mock the patch extractor
        mock_patch_extractor = Mock()
        mock_patch_extractor.session_id = "patch-extractor-id"

        # Mock the volume
        mock_volume = Mock()
        mock_docker_client = Mock()
        mock_docker_client.volumes.get.return_value = mock_volume

        mock_cmd_executor.client = mock_docker_client

        # Setup get_label to return volume name and patch extractor ID
        def get_label_side_effect(label):
            if label == DAIV_SANDBOX_WORKDIR_VOLUME_LABEL:
                return "test-volume-name"
            elif label == DAIV_SANDBOX_PATCH_EXTRACTOR_SESSION_ID_LABEL:
                return "patch-extractor-id"
            return None

        mock_cmd_executor.get_label.side_effect = get_label_side_effect

        # Setup the mock to return instances
        mock_session_class.side_effect = [mock_cmd_executor, mock_patch_extractor]

        # Make the request
        response = client.delete("/session/cmd-executor-id/")

        assert response.status_code == 204

        # Verify volume was retrieved and removed
        mock_docker_client.volumes.get.assert_called_once_with("test-volume-name")
        mock_volume.remove.assert_called_once_with(force=False)

        # Verify both containers were removed
        mock_patch_extractor.remove_container.assert_called_once()
        mock_cmd_executor.remove_container.assert_called_once()


def test_close_session_handles_missing_volume(client):
    """Test that closing a session handles missing volumes gracefully."""
    from docker.errors import NotFound

    from daiv_sandbox.main import DAIV_SANDBOX_PATCH_EXTRACTOR_SESSION_ID_LABEL, DAIV_SANDBOX_WORKDIR_VOLUME_LABEL

    with patch("daiv_sandbox.main.SandboxDockerSession") as mock_session_class:
        # Mock the cmd executor
        mock_cmd_executor = Mock()
        mock_cmd_executor.session_id = "cmd-executor-id"
        mock_cmd_executor.container = Mock()

        # Mock the docker client
        mock_docker_client = Mock()
        mock_docker_client.volumes.get.side_effect = NotFound("Volume not found")

        mock_cmd_executor.client = mock_docker_client

        # Setup get_label to return volume name but no patch extractor
        def get_label_side_effect(label):
            if label == DAIV_SANDBOX_WORKDIR_VOLUME_LABEL:
                return "test-volume-name"
            elif label == DAIV_SANDBOX_PATCH_EXTRACTOR_SESSION_ID_LABEL:
                return None
            return None

        mock_cmd_executor.get_label.side_effect = get_label_side_effect

        # Setup the mock to return instance
        mock_session_class.return_value = mock_cmd_executor

        # Make the request - should succeed even though volume is not found
        response = client.delete("/session/cmd-executor-id/")

        assert response.status_code == 204

        # Verify we tried to get the volume
        mock_docker_client.volumes.get.assert_called_once_with("test-volume-name")

        # Verify container was still removed
        mock_cmd_executor.remove_container.assert_called_once()
