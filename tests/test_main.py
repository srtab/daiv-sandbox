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
