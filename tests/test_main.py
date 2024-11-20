import base64
import io
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from daiv_sandbox import __version__
from daiv_sandbox.config import settings
from daiv_sandbox.main import app
from daiv_sandbox.schemas import RunResult


@pytest.fixture
def client():
    return TestClient(app, headers={"X-API-Key": settings.API_KEY}, root_path=settings.API_V1_STR)


def test_missing_api_key(client):
    # Send a request without the API key
    client.headers = {}
    response = client.post("/run/commands/", json={})
    assert response.status_code == 403
    assert response.json() == {"detail": "API Key header is missing"}


def test_invalid_api_key(client):
    # Send a request with an invalid API key
    client.headers["X-API-Key"] = "invalid_key"
    response = client.post("/run/commands/", json={})
    assert response.status_code == 403
    assert response.json() == {"detail": "Invalid API Key"}


@patch("daiv_sandbox.main.SandboxDockerSession")
def test_run_commands_success(MockSession, client):  # noqa: N803
    # Mock the session and its methods
    mock_session = MockSession.return_value.__enter__.return_value
    mock_session.execute_command.return_value = RunResult(
        command="echo 'Hello, World!'", output=b"success", exit_code=0, changed_files=[]
    )
    # Use a valid Base64-encoded string
    mock_session.create_tar_gz_archive.return_value = io.BytesIO(b"mocked_archive")

    # Create a request payload with a valid UUID4
    request_payload = {
        "run_id": str(uuid.uuid4()),  # Generate a valid UUID4
        "base_image": "python:3.9",
        "commands": ["echo 'Hello, World!'"],
        "archive": base64.b64encode(b"test").decode(),  # Base64 for "test"
    }

    # Send a POST request to the endpoint
    response = client.post("/run/commands/", json=request_payload)

    # Assert the response
    assert response.status_code == 200, response.text
    response_data = response.json()
    assert "results" in response_data
    assert "archive" in response_data
    assert response_data["results"][0]["output"] == "success"
    assert response_data["results"][0]["exit_code"] == 0


@patch("daiv_sandbox.main.SandboxDockerSession")
def test_run_commands_failure(MockSession, client):  # noqa: N803
    # Mock the session and its methods
    mock_session = MockSession.return_value.__enter__.return_value
    mock_session.execute_command.return_value = RunResult(
        command="exit 1", output=b"error", exit_code=1, changed_files=[]
    )
    # Use a valid Base64-encoded string
    mock_session.create_tar_gz_archive.return_value = io.BytesIO(b"mocked_archive")

    # Create a request payload with a valid UUID4
    request_payload = {
        "run_id": str(uuid.uuid4()),  # Generate a valid UUID4
        "base_image": "python:3.9",
        "commands": ["exit 1"],
        "archive": base64.b64encode(b"test").decode(),  # Base64 for "test"
    }

    # Send a POST request to the endpoint
    response = client.post("/run/commands/", json=request_payload)

    # Assert the response
    assert response.status_code == 200, response.text
    response_data = response.json()
    assert "results" in response_data
    assert "archive" in response_data
    assert response_data["results"][0]["output"] == "error"
    assert response_data["results"][0]["exit_code"] == 1


@patch("daiv_sandbox.main.SandboxDockerSession")
def test_run_commands_with_workdir(MockSession, client):  # noqa: N803
    # Mock the session and its methods
    mock_session = MockSession.return_value.__enter__.return_value
    mock_session.execute_command.return_value = RunResult(
        command="echo 'Hello, World!'", output=b"success", exit_code=0, changed_files=[]
    )
    mock_session.create_tar_gz_archive.return_value = io.BytesIO(b"mocked_archive")

    # Create a request payload with a valid UUID4 and workdir
    request_payload = {
        "run_id": str(uuid.uuid4()),
        "base_image": "python:3.9",
        "commands": ["echo 'Hello, World!'"],
        "archive": base64.b64encode(b"test").decode(),
        "workdir": "/app",
    }

    # Send a POST request to the endpoint
    response = client.post("/run/commands/", json=request_payload)

    # Assert the response
    assert response.status_code == 200, response.text
    response_data = response.json()
    assert "results" in response_data
    assert "archive" in response_data
    assert response_data["results"][0]["output"] == "success"
    assert response_data["results"][0]["exit_code"] == 0


def test_health(client):
    response = client.get("/health/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_version(client):
    response = client.get("/version/")
    assert response.status_code == 200
    assert response.json() == {"version": __version__}
