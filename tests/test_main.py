import base64
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from daiv_sandbox.main import app


@pytest.fixture
def client():
    return TestClient(app)


@patch("daiv_sandbox.main.SandboxDockerSession")
def test_run_commands_success(MockSession, client):  # noqa: N803
    # Mock the session and its methods
    mock_session = MockSession.return_value.__enter__.return_value
    mock_session.execute_command.return_value = MagicMock(output=b"success", exit_code=0)
    # Use a valid Base64-encoded string
    mock_session.extract_changed_files.return_value = base64.b64encode(b"mocked_archive")

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
    assert response_data["results"]["echo 'Hello, World!'"]["output"] == "success"
    assert response_data["results"]["echo 'Hello, World!'"]["exit_code"] == 0


@patch("daiv_sandbox.main.SandboxDockerSession")
def test_run_commands_failure(MockSession, client):  # noqa: N803
    # Mock the session and its methods
    mock_session = MockSession.return_value.__enter__.return_value
    mock_session.execute_command.return_value = MagicMock(output=b"error", exit_code=1)
    # Use a valid Base64-encoded string
    mock_session.extract_changed_files.return_value = base64.b64encode(b"mocked_archive")

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
    assert response_data["results"]["exit 1"]["output"] == "error"
    assert response_data["results"]["exit 1"]["exit_code"] == 1
