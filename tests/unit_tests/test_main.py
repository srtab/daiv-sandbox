import base64
import io
import uuid
from contextlib import AbstractAsyncContextManager
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from daiv_sandbox import __version__
from daiv_sandbox.config import settings
from daiv_sandbox.locks import SessionBusyError
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
    # Keep the suite hermetic: lifespan starts the background reaper, which would otherwise spin up a
    # real Docker client. Tests that exercise the reaper patch start_reaper themselves.
    with (
        patch("daiv_sandbox.main.start_reaper", return_value=None),
        TestClient(
            app, headers={"X-API-Key": settings.API_KEY.get_secret_value()}, root_path=settings.API_V1_STR
        ) as client,
    ):
        yield client


class _BusyLockContext(AbstractAsyncContextManager[None]):
    def __init__(self, session_id: str):
        self.session_id = session_id

    async def __aenter__(self) -> None:
        raise SessionBusyError(self.session_id)

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class BusySessionLockManager:
    def acquire(self, session_id: str) -> _BusyLockContext:
        return _BusyLockContext(session_id)


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


def test_run_commands_timeout_returns_124(mock_session, client):
    """A command that exceeds its timeout is reported with exit code 124 and a 'timed out' message.

    `execute_command` raising TimeoutError stands in for the wall-clock timeout: asyncio.wait_for
    propagates it into the same handler that a real timeout hits."""
    mock_session.execute_command.side_effect = TimeoutError
    resp = client.post(f"/session/{mock_session.session_id}/", json={"commands": ["sleep 99"], "timeout": 5})
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert len(results) == 1
    assert results[0]["exit_code"] == 124
    assert "timed out after 5s" in results[0]["output"].lower()


def test_run_commands_timeout_stops_remaining_commands(mock_session, client):
    """A timeout terminates the sequence: later commands in the same request are not executed."""
    mock_session.execute_command.side_effect = [
        TimeoutError,
        RunResult(command="cmd2", output="should not run", exit_code=0, workdir="/workspace/repo"),
    ]
    resp = client.post(f"/session/{mock_session.session_id}/", json={"commands": ["cmd1", "cmd2"], "timeout": 3})
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert len(results) == 1
    assert results[0]["exit_code"] == 124
    assert mock_session.execute_command.call_count == 1


def test_run_commands_request_timeout_overrides_server_default(mock_session, client, monkeypatch):
    """The request `timeout` wins over the server default (DAIV_SANDBOX_COMMAND_TIMEOUT)."""
    monkeypatch.setattr(settings, "COMMAND_TIMEOUT", 99)
    mock_session.execute_command.side_effect = TimeoutError
    resp = client.post(f"/session/{mock_session.session_id}/", json={"commands": ["x"], "timeout": 5})
    assert resp.status_code == 200, resp.text
    assert "timed out after 5s" in resp.json()["results"][0]["output"].lower()


def test_run_commands_uses_server_default_timeout_when_unset(mock_session, client, monkeypatch):
    """Omitting `timeout` falls back to the server default."""
    monkeypatch.setattr(settings, "COMMAND_TIMEOUT", 7)
    mock_session.execute_command.side_effect = TimeoutError
    resp = client.post(f"/session/{mock_session.session_id}/", json={"commands": ["x"]})
    assert resp.status_code == 200, resp.text
    assert "timed out after 7s" in resp.json()["results"][0]["output"].lower()


def test_run_commands_zero_timeout_disables_timeout(mock_session, client, monkeypatch):
    """timeout=0 (and a 0 server default) means no timeout is enforced: a normal command succeeds
    rather than every command being killed."""
    monkeypatch.setattr(settings, "COMMAND_TIMEOUT", 0)
    mock_session.execute_command.return_value = RunResult(
        command="echo", output="ok", exit_code=0, workdir="/workspace/repo"
    )
    resp = client.post(f"/session/{mock_session.session_id}/", json={"commands": ["echo"], "timeout": 0})
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert len(results) == 1
    assert results[0]["exit_code"] == 0
    assert results[0]["output"] == "ok"


def test_run_commands_returns_conflict_when_session_is_locked(mock_session, client, monkeypatch):
    monkeypatch.setattr(app.state, "session_lock_manager", BusySessionLockManager())

    response = client.post(f"/session/{mock_session.session_id}/", json={"commands": ["echo hello"]})

    assert response.status_code == 409
    assert response.json() == {"detail": "Session is busy"}


def test_health(client):
    with patch("daiv_sandbox.main.SandboxDockerSession.ping", return_value=True):
        response = client.get("/-/health/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_unhealthy(client):
    with patch("daiv_sandbox.main.SandboxDockerSession.ping", return_value=False):
        response = client.get("/-/health/")
    assert response.status_code == 503
    assert response.json() == {"detail": "Docker client is not responding"}


def test_version(client):
    response = client.get("/-/version/")
    assert response.status_code == 200
    assert response.json() == {"version": __version__}


def test_start_session_starts_single_container(client):
    """A session start creates exactly one cmd-executor container — no sidecar, no volume."""
    with patch("daiv_sandbox.main.SandboxDockerSession") as mock_session_class:
        mock_cmd_executor = Mock()
        mock_cmd_executor.session_id = "cmd-executor-id"
        mock_session_class.start.return_value = mock_cmd_executor

        response = client.post("/session/", json={"base_image": "python:3.11"})

        assert response.status_code == 200
        assert response.json() == {"session_id": "cmd-executor-id"}

        # Exactly one container started, with no volume mount and no sidecar.
        mock_session_class.start.assert_called_once()
        assert "volumes" not in mock_session_class.start.call_args.kwargs
        # No shared volume is created (the sidecar/volume path is gone).
        mock_session_class.create_named_volume.assert_not_called()


def test_start_session_isolates_network_by_default(client):
    """Without network_enabled, the container is started with network_mode='none' (a security
    control): a regression that silently enabled the network would be caught here."""
    with patch("daiv_sandbox.main.SandboxDockerSession") as mock_session_class:
        mock_cmd_executor = Mock()
        mock_cmd_executor.session_id = "id"
        mock_session_class.start.return_value = mock_cmd_executor

        response = client.post("/session/", json={"base_image": "python:3.12"})

        assert response.status_code == 200, response.text
        assert mock_session_class.start.call_args.kwargs["network_mode"] == "none"


def test_start_session_passes_resource_limits(client):
    """memory_bytes/cpus/environment map onto the container start; network_enabled drops the
    network_mode isolation."""
    with patch("daiv_sandbox.main.SandboxDockerSession") as mock_session_class:
        mock_cmd_executor = Mock()
        mock_cmd_executor.session_id = "id"
        mock_session_class.start.return_value = mock_cmd_executor

        response = client.post(
            "/session/",
            json={
                "base_image": "python:3.12",
                "network_enabled": True,
                "memory_bytes": 1024,
                "cpus": 1.5,
                "environment": {"FOO": "bar"},
            },
        )

        assert response.status_code == 200, response.text
        kwargs = mock_session_class.start.call_args.kwargs
        assert kwargs["mem_limit"] == 1024
        assert kwargs["cpus"] == 1.5
        assert kwargs["environment"] == {"FOO": "bar"}
        assert "network_mode" not in kwargs  # network_enabled=True -> no isolation


def test_start_session_attaches_configured_network_when_enabled(client, monkeypatch):
    """A network-enabled session attaches to the Docker network named by DAIV_SANDBOX_NETWORK when
    one is configured (and never sets network_mode, which would conflict with an explicit network)."""
    monkeypatch.setattr(settings, "NETWORK", "daiv-net")
    with patch("daiv_sandbox.main.SandboxDockerSession") as mock_session_class:
        mock_cmd_executor = Mock()
        mock_cmd_executor.session_id = "id"
        mock_session_class.start.return_value = mock_cmd_executor

        response = client.post("/session/", json={"base_image": "python:3.12", "network_enabled": True})

        assert response.status_code == 200, response.text
        kwargs = mock_session_class.start.call_args.kwargs
        assert kwargs.get("network") == "daiv-net"
        assert "network_mode" not in kwargs


def test_start_session_enabled_without_configured_network_uses_default(client, monkeypatch):
    """A network-enabled session with no DAIV_SANDBOX_NETWORK configured falls back to Docker's
    default network: neither an explicit `network` nor a `network_mode` kwarg is passed."""
    monkeypatch.setattr(settings, "NETWORK", None)
    with patch("daiv_sandbox.main.SandboxDockerSession") as mock_session_class:
        mock_cmd_executor = Mock()
        mock_cmd_executor.session_id = "id"
        mock_session_class.start.return_value = mock_cmd_executor

        response = client.post("/session/", json={"base_image": "python:3.12", "network_enabled": True})

        assert response.status_code == 200, response.text
        kwargs = mock_session_class.start.call_args.kwargs
        assert "network" not in kwargs
        assert "network_mode" not in kwargs


def test_start_session_disabled_network_ignores_configured_network(client, monkeypatch):
    """Security corner: a session that does NOT enable networking stays isolated with
    network_mode='none' even when DAIV_SANDBOX_NETWORK is configured — isolation takes precedence and
    the named network is not attached."""
    monkeypatch.setattr(settings, "NETWORK", "daiv-net")
    with patch("daiv_sandbox.main.SandboxDockerSession") as mock_session_class:
        mock_cmd_executor = Mock()
        mock_cmd_executor.session_id = "id"
        mock_session_class.start.return_value = mock_cmd_executor

        response = client.post("/session/", json={"base_image": "python:3.12", "network_enabled": False})

        assert response.status_code == 200, response.text
        kwargs = mock_session_class.start.call_args.kwargs
        assert kwargs["network_mode"] == "none"
        assert "network" not in kwargs


def test_close_session_stops_container_by_default(client):
    """Closing a session stops (not removes) the container, preserving it for warm reuse."""
    with patch("daiv_sandbox.main.SandboxDockerSession") as mock_session_class:
        mock_cmd_executor = Mock()
        mock_cmd_executor.session_id = "cmd-executor-id"
        mock_session_class.return_value = mock_cmd_executor

        response = client.delete("/session/cmd-executor-id/")

        assert response.status_code == 204
        mock_cmd_executor.stop_container.assert_called_once()
        mock_cmd_executor.remove_container.assert_not_called()


def test_close_session_force_removes_container(client):
    """DELETE ?force=true removes the container immediately."""
    with patch("daiv_sandbox.main.SandboxDockerSession") as mock_session_class:
        mock_cmd_executor = Mock()
        mock_cmd_executor.session_id = "cmd-executor-id"
        mock_session_class.return_value = mock_cmd_executor

        response = client.delete("/session/cmd-executor-id/?force=true")

        assert response.status_code == 204
        mock_cmd_executor.remove_container.assert_called_once()
        mock_cmd_executor.stop_container.assert_not_called()


def test_get_session_returns_204_when_present(client):
    with patch("daiv_sandbox.main.SandboxDockerSession") as mock_session_class:
        instance = mock_session_class.return_value
        instance.container = Mock()
        response = client.get("/session/some-id/")
        assert response.status_code == 204


def test_get_session_returns_404_when_missing(client):
    with patch("daiv_sandbox.main.SandboxDockerSession") as mock_session_class:
        instance = mock_session_class.return_value
        instance.container = None
        response = client.get("/session/missing-id/")
        assert response.status_code == 404


def test_lifespan_starts_and_cancels_reaper():
    """The app lifespan schedules the reaper on startup and cancels it on shutdown."""
    started = {}

    def fake_start_reaper(app):
        task = Mock()
        started["task"] = task
        return task

    with (
        patch("daiv_sandbox.main.start_reaper", side_effect=fake_start_reaper) as start_mock,
        TestClient(app, headers={"X-API-Key": settings.API_KEY.get_secret_value()}, root_path=settings.API_V1_STR),
    ):
        pass  # enter+exit runs startup then shutdown

    start_mock.assert_called_once()
    started["task"].cancel.assert_called_once()


def test_close_session_returns_conflict_when_session_is_locked(mock_session, client, monkeypatch):
    monkeypatch.setattr(app.state, "session_lock_manager", BusySessionLockManager())

    response = client.delete(f"/session/{mock_session.session_id}/")

    assert response.status_code == 409
    assert response.json() == {"detail": "Session is busy"}


def _minimal_archive_bytes(member_name: str = "README.md", member_content: bytes = b"hello") -> bytes:
    """Build a minimal uncompressed tar archive with a single member."""
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name=member_name)
        info.size = len(member_content)
        tf.addfile(info, io.BytesIO(member_content))
    return buf.getvalue()


def test_seed_session_extracts_repo_archive(mock_session, client):
    """repo_archive only: copy_to_container is called once with /workspace/repo as dest."""
    from docker.models.containers import ExecResult

    from daiv_sandbox.sessions import SANDBOX_ROOT

    mock_session.container.exec_run.side_effect = [
        ExecResult(exit_code=1, output=b""),
        ExecResult(exit_code=0, output=b""),
    ]

    resp = client.post(
        f"/session/{mock_session.session_id}/seed/",
        files={"repo_archive": ("repo.tar", _minimal_archive_bytes(), "application/x-tar")},
    )
    assert resp.status_code == 204, resp.text
    mock_session.copy_to_container.assert_called_once()
    assert mock_session.copy_to_container.call_args.kwargs.get("dest") == SANDBOX_ROOT


def test_seed_session_rejects_unknown_session(client):
    """When the session has no container, return 404."""
    with patch("daiv_sandbox.main.SandboxDockerSession") as cls:
        instance = cls.return_value
        instance.container = None
        resp = client.post(
            "/session/does-not-exist/seed/",
            files={"repo_archive": ("repo.tar", _minimal_archive_bytes(), "application/x-tar")},
        )
        assert resp.status_code == 404


def test_seed_session_rejects_double_seed(mock_session, client):
    """A second seed attempt on an already-seeded session returns 409."""
    from docker.models.containers import ExecResult

    mock_session.container.exec_run.return_value = ExecResult(exit_code=0, output=b"")
    resp = client.post(
        f"/session/{mock_session.session_id}/seed/",
        files={"repo_archive": ("repo.tar", _minimal_archive_bytes(), "application/x-tar")},
    )
    assert resp.status_code == 409
    assert resp.json() == {"detail": "Session already seeded"}


def test_seed_session_extracts_skills_archive_only(mock_session, client):
    """skills_archive only: copy_to_container hits /skills; seed runs no in-container commands."""
    from docker.models.containers import ExecResult

    from daiv_sandbox.sessions import SKILLS_ROOT

    mock_session.container.exec_run.side_effect = [
        ExecResult(exit_code=1, output=b""),
        ExecResult(exit_code=0, output=b""),
    ]

    resp = client.post(
        f"/session/{mock_session.session_id}/seed/",
        files={"skills_archive": ("skills.tar", _minimal_archive_bytes("intro.md", b"# hi\n"), "application/x-tar")},
    )
    assert resp.status_code == 204, resp.text
    mock_session.copy_to_container.assert_called_once()
    assert mock_session.copy_to_container.call_args.kwargs.get("dest") == SKILLS_ROOT
    # Seeding only copies archives + writes the marker (via exec_run); never execute_command.
    mock_session.execute_command.assert_not_called()


def test_seed_session_extracts_both_archives(mock_session, client):
    """Both archives: copy_to_container called twice with the right destinations, in order."""
    from docker.models.containers import ExecResult

    from daiv_sandbox.sessions import SANDBOX_ROOT, SKILLS_ROOT

    mock_session.container.exec_run.side_effect = [
        ExecResult(exit_code=1, output=b""),
        ExecResult(exit_code=0, output=b""),
    ]

    resp = client.post(
        f"/session/{mock_session.session_id}/seed/",
        files=[
            ("repo_archive", ("repo.tar", _minimal_archive_bytes(), "application/x-tar")),
            ("skills_archive", ("skills.tar", _minimal_archive_bytes("intro.md", b"# hi\n"), "application/x-tar")),
        ],
    )
    assert resp.status_code == 204, resp.text
    assert mock_session.copy_to_container.call_count == 2
    dests = [call.kwargs.get("dest") for call in mock_session.copy_to_container.call_args_list]
    assert dests == [SANDBOX_ROOT, SKILLS_ROOT]


def test_seed_session_requires_at_least_one_archive(mock_session, client):
    """Posting neither archive returns 422."""
    resp = client.post(f"/session/{mock_session.session_id}/seed/")
    assert resp.status_code == 422
    assert "at least one" in resp.json()["detail"].lower()


def test_seed_session_invalid_repo_archive_returns_422(mock_session, client):
    """copy_to_container raising ValueError for repo_archive returns 422."""
    from docker.models.containers import ExecResult

    mock_session.container.exec_run.return_value = ExecResult(exit_code=1, output=b"")
    mock_session.copy_to_container.side_effect = ValueError("Invalid or truncated archive: ...")

    resp = client.post(
        f"/session/{mock_session.session_id}/seed/",
        files={"repo_archive": ("repo.tar", b"not a tar", "application/x-tar")},
    )
    assert resp.status_code == 422
    assert "repo_archive" in resp.json()["detail"].lower()


def test_seed_session_invalid_skills_archive_returns_422(mock_session, client):
    """copy_to_container raising ValueError for skills_archive returns 422."""
    from docker.models.containers import ExecResult

    mock_session.container.exec_run.return_value = ExecResult(exit_code=1, output=b"")
    mock_session.copy_to_container.side_effect = ValueError("Invalid or truncated archive: ...")

    resp = client.post(
        f"/session/{mock_session.session_id}/seed/",
        files={"skills_archive": ("skills.tar", b"garbage", "application/x-tar")},
    )
    assert resp.status_code == 422
    assert "skills_archive" in resp.json()["detail"].lower()


def test_seed_session_marker_write_failure_returns_500(mock_session, client):
    """If the seeded-marker touch fails, seed returns 500."""
    from docker.models.containers import ExecResult

    mock_session.container.exec_run.side_effect = [
        ExecResult(exit_code=1, output=b""),  # seeded check → not seeded
        ExecResult(exit_code=1, output=b"read-only filesystem"),  # marker write fails
    ]

    resp = client.post(
        f"/session/{mock_session.session_id}/seed/",
        files={"repo_archive": ("repo.tar", _minimal_archive_bytes(), "application/x-tar")},
    )
    assert resp.status_code == 500
    assert "seeded" in resp.json()["detail"].lower()


def test_fs_write_then_read_roundtrip(mock_session, client):
    mock_session.write_file.return_value = None
    mock_session.read_file_bytes.return_value = b"hello\n"
    sid = mock_session.session_id

    w = client.post(
        f"/session/{sid}/fs/write",
        json={"path": "/workspace/tmp/a.txt", "content": base64.b64encode(b"hello\n").decode(), "mode": 0o644},
    )
    assert w.status_code == 200, w.text
    assert w.json() == {"ok": True, "error": None}

    r = client.post(f"/session/{sid}/fs/read", json={"path": "/workspace/tmp/a.txt", "offset": 0, "limit": 2000})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["encoding"] == "utf-8"
    assert "hello" in body["content"]


def test_fs_write_accepts_repo_subdir(mock_session, client):
    """A write to /workspace/repo is now accepted (fs/* spans the whole workspace)."""
    mock_session.write_file.return_value = None
    resp = client.post(
        f"/session/{mock_session.session_id}/fs/write",
        json={"path": "/workspace/repo/x.txt", "content": base64.b64encode(b"hi").decode(), "mode": 0o644},
    )
    assert resp.status_code == 200 and resp.json()["ok"] is True, resp.text


def test_fs_write_rejects_outside_workspace(mock_session, client):
    """A path outside /workspace is rejected (returned as ok=False, not an HTTP error)."""
    resp = client.post(
        f"/session/{mock_session.session_id}/fs/write",
        json={"path": "/etc/evil", "content": base64.b64encode(b"x").decode(), "mode": 0o644},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is False
    mock_session.write_file.assert_not_called()


def test_fs_write_rejects_bare_workspace_root(mock_session, client):
    """File ops forbid the bare /workspace root (allow_root=False); only ls/grep/glob may target it."""
    resp = client.post(
        f"/session/{mock_session.session_id}/fs/write",
        json={"path": "/workspace", "content": base64.b64encode(b"x").decode(), "mode": 0o644},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is False
    mock_session.write_file.assert_not_called()


def test_fs_write_rejects_path_outside_workspace_repo_sibling(mock_session, client):
    """A pre-reparent path like /repo (now outside /workspace) is rejected."""
    sid = mock_session.session_id
    resp = client.post(
        f"/session/{sid}/fs/write",
        json={"path": "/repo/evil.py", "content": base64.b64encode(b"x").decode(), "mode": 0o644},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is False
    assert resp.json()["error"]["code"] == "invalid_path"
    assert "under" in resp.json()["error"]["message"]
    mock_session.write_file.assert_not_called()


def test_fs_write_conflict_returns_quiet_error(mock_session, client):
    """A create-only conflict (write_file raises FileExistsError) is surfaced as ok=False with the
    deepagents message — not an HTTP error."""
    mock_session.write_file.side_effect = FileExistsError(
        "Cannot write to /workspace/tmp/a.txt because it already exists. "
        "Read and then make an edit, or write to a new path."
    )
    resp = client.post(
        f"/session/{mock_session.session_id}/fs/write",
        json={"path": "/workspace/tmp/a.txt", "content": base64.b64encode(b"x").decode(), "mode": 0o644},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "already_exists"
    assert "already exists" in body["error"]["message"]


def test_fs_write_passes_create_only(mock_session, client):
    """fs/write requests create-only semantics from the session layer."""
    mock_session.write_file.return_value = None
    resp = client.post(
        f"/session/{mock_session.session_id}/fs/write",
        json={"path": "/workspace/tmp/a.txt", "content": base64.b64encode(b"x").decode(), "mode": 0o644},
    )
    assert resp.status_code == 200, resp.text
    assert mock_session.write_file.call_args.kwargs.get("create_only") is True


def test_fs_ls_returns_entries(mock_session, client):
    mock_session.list_dir.return_value = [("/workspace/tmp/sub", True), ("/workspace/tmp/f.py", False)]
    resp = client.post(f"/session/{mock_session.session_id}/fs/ls", json={"path": "/workspace/tmp"})
    assert resp.status_code == 200, resp.text
    assert {"path": "/workspace/tmp/sub", "is_dir": True} in resp.json()["entries"]


@pytest.mark.parametrize("op", ["ls", "grep", "glob"])
def test_fs_dir_endpoints_reject_traversal(mock_session, client, op):
    """ls/grep/glob reject `..` traversal as a 200 body with error.code=invalid_path (unified with
    the file ops, which already returned 200)."""
    payload = {"path": "/workspace/tmp/../repo"}
    if op in ("grep", "glob"):
        payload["pattern"] = "x"
    resp = client.post(f"/session/{mock_session.session_id}/fs/{op}", json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["error"]["code"] == "invalid_path"


def test_fs_ls_surfaces_command_error(mock_session, client):
    mock_session.list_dir.side_effect = RuntimeError("ls failed (exit 2) for '/workspace/tmp/x'")
    resp = client.post(f"/session/{mock_session.session_id}/fs/ls", json={"path": "/workspace/tmp/x"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["entries"] == []
    assert body["error"]["code"] == "exec_failed"


def test_fs_ls_missing_returns_not_found(mock_session, client):
    mock_session.list_dir.side_effect = FileNotFoundError("/workspace/repo/nope")
    resp = client.post(f"/session/{mock_session.session_id}/fs/ls", json={"path": "/workspace/repo/nope"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["entries"] == []
    assert body["error"]["code"] == "not_found"


def test_fs_ls_on_file_returns_not_a_directory(mock_session, client):
    mock_session.list_dir.side_effect = NotADirectoryError("/workspace/repo/f.txt")
    resp = client.post(f"/session/{mock_session.session_id}/fs/ls", json={"path": "/workspace/repo/f.txt"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["error"]["code"] == "not_a_directory"


def test_fs_ls_invalid_path_returns_200_invalid_path(mock_session, client):
    resp = client.post(f"/session/{mock_session.session_id}/fs/ls", json={"path": "/workspace/tmp/../repo"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["error"]["code"] == "invalid_path"
    mock_session.list_dir.assert_not_called()


def test_fs_read_directory_returns_is_a_directory(mock_session, client):
    mock_session.read_file_bytes.side_effect = IsADirectoryError("/workspace/repo/src")
    resp = client.post(
        f"/session/{mock_session.session_id}/fs/read", json={"path": "/workspace/repo/src", "offset": 0, "limit": 2000}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["error"]["code"] == "is_a_directory"


def test_fs_delete_reports_removed_flag(mock_session, client):
    mock_session.delete_file.return_value = False
    resp = client.post(f"/session/{mock_session.session_id}/fs/delete", json={"path": "/workspace/tmp/gone.txt"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["removed"] is False


def test_fs_delete_directory_returns_is_a_directory(mock_session, client):
    mock_session.delete_file.side_effect = IsADirectoryError("/workspace/tmp/adir")
    resp = client.post(f"/session/{mock_session.session_id}/fs/delete", json={"path": "/workspace/tmp/adir"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "is_a_directory"


def test_fs_grep_surfaces_command_error(mock_session, client):
    mock_session.grep.side_effect = RuntimeError("grep failed (exit 2)")
    resp = client.post(f"/session/{mock_session.session_id}/fs/grep", json={"path": "/workspace/tmp", "pattern": "x"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["matches"] == []
    assert body["error"]["code"] == "exec_failed"


def test_fs_grep_missing_directory_returns_empty(mock_session, client):
    """A missing search path is now surfaced as not_found."""
    mock_session.grep.side_effect = FileNotFoundError("/workspace/repo/.claude/skills")
    resp = client.post(
        f"/session/{mock_session.session_id}/fs/grep", json={"path": "/workspace/repo/.claude/skills", "pattern": "x"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["matches"] == []
    assert body["error"]["code"] == "not_found"


def test_fs_glob_unbalanced_bracket_is_literal(mock_session, client):
    """An unbalanced '[' is treated as a literal (shell-like), not rejected as a 400.

    glob.translate never raises on malformed bracket classes, so the pattern compiles and matches a
    file literally named '[' rather than producing an error.
    """
    from daiv_sandbox.sessions import DirEntry

    mock_session.find_paths.return_value = [DirEntry("/workspace/tmp/[", False), DirEntry("/workspace/tmp/a.py", False)]
    resp = client.post(f"/session/{mock_session.session_id}/fs/glob", json={"path": "/workspace/tmp", "pattern": "["})
    assert resp.status_code == 200, resp.text
    assert [e["path"] for e in resp.json()["matches"]] == ["/workspace/tmp/["]


def test_fs_glob_filters_by_pattern(mock_session, client):
    from daiv_sandbox.sessions import DirEntry

    mock_session.find_paths.return_value = [
        DirEntry("/workspace/tmp/a.py", False),
        DirEntry("/workspace/tmp/b.txt", False),
        DirEntry("/workspace/tmp/sub", True),
    ]
    resp = client.post(
        f"/session/{mock_session.session_id}/fs/glob", json={"path": "/workspace/tmp", "pattern": "*.py"}
    )
    assert resp.status_code == 200, resp.text
    assert [e["path"] for e in resp.json()["matches"]] == ["/workspace/tmp/a.py"]


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("[ab].py", ["/workspace/tmp/a.py", "/workspace/tmp/b.py"]),  # class membership
        ("[!a]*.py", ["/workspace/tmp/b.py", "/workspace/tmp/c.py"]),  # negated class
    ],
)
def test_fs_glob_char_classes(mock_session, client, pattern, expected):
    """Bracket classes work end-to-end through glob.translate: `[abc]` membership and `[!x]` negation.

    Guards the semantics that changed when `_glob_to_regex` moved off the hand-rolled engine, so a
    future swap back to e.g. fnmatch can't silently alter char-class matching.
    """
    from daiv_sandbox.sessions import DirEntry

    mock_session.find_paths.return_value = [
        DirEntry("/workspace/tmp/a.py", False),
        DirEntry("/workspace/tmp/b.py", False),
        DirEntry("/workspace/tmp/c.py", False),
    ]
    resp = client.post(
        f"/session/{mock_session.session_id}/fs/glob", json={"path": "/workspace/tmp", "pattern": pattern}
    )
    assert resp.status_code == 200, resp.text
    assert sorted(e["path"] for e in resp.json()["matches"]) == sorted(expected)


def test_fs_glob_surfaces_find_error(mock_session, client):
    mock_session.find_paths.side_effect = RuntimeError("find failed")
    resp = client.post(f"/session/{mock_session.session_id}/fs/glob", json={"path": "/workspace/tmp/x", "pattern": "*"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["error"]["code"] == "exec_failed"


def test_fs_glob_missing_directory_returns_empty(mock_session, client):
    """A missing base directory is now surfaced as not_found."""
    mock_session.find_paths.side_effect = FileNotFoundError("/workspace/repo/.cursor/skills")
    resp = client.post(
        f"/session/{mock_session.session_id}/fs/glob",
        json={"path": "/workspace/repo/.cursor/skills", "pattern": "**/*"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["matches"] == []
    assert body["error"]["code"] == "not_found"


def test_fs_glob_results_are_sorted(mock_session, client):
    """Glob matches are returned sorted by path (deterministic, matching deepagents' sorted glob),
    regardless of the order find_paths enumerated them."""
    from daiv_sandbox.sessions import DirEntry

    mock_session.find_paths.return_value = [
        DirEntry("/workspace/tmp/c.py", False),
        DirEntry("/workspace/tmp/a.py", False),
        DirEntry("/workspace/tmp/b.py", False),
    ]
    resp = client.post(
        f"/session/{mock_session.session_id}/fs/glob", json={"path": "/workspace/tmp", "pattern": "*.py"}
    )
    assert resp.status_code == 200, resp.text
    assert [e["path"] for e in resp.json()["matches"]] == [
        "/workspace/tmp/a.py",
        "/workspace/tmp/b.py",
        "/workspace/tmp/c.py",
    ]


def test_fs_read_missing_file(mock_session, client):
    mock_session.read_file_bytes.side_effect = FileNotFoundError("/workspace/tmp/x")
    resp = client.post(f"/session/{mock_session.session_id}/fs/read", json={"path": "/workspace/tmp/x"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["error"]["code"] == "not_found"


def test_fs_read_empty_file(mock_session, client):
    mock_session.read_file_bytes.return_value = b""
    resp = client.post(f"/session/{mock_session.session_id}/fs/read", json={"path": "/workspace/tmp/e.txt"})
    body = resp.json()
    assert body["encoding"] == "utf-8"
    assert "empty" in body["content"].lower()


def test_fs_read_binary_falls_back_to_base64(mock_session, client):
    mock_session.read_file_bytes.return_value = b"\xff\xfe\x00"
    resp = client.post(f"/session/{mock_session.session_id}/fs/read", json={"path": "/workspace/tmp/b.bin"})
    body = resp.json()
    assert body["encoding"] == "base64"
    assert base64.b64decode(body["content"]) == b"\xff\xfe\x00"


def test_fs_read_offset_beyond_eof(mock_session, client):
    mock_session.read_file_bytes.return_value = b"one\ntwo\n"
    resp = client.post(
        f"/session/{mock_session.session_id}/fs/read", json={"path": "/workspace/tmp/a.txt", "offset": 50, "limit": 10}
    )
    assert resp.json()["error"]["code"] == "invalid_offset"
    assert "offset" in resp.json()["error"]["message"].lower()


def test_fs_read_text_truncated_at_cap(mock_session, client):
    """A text page larger than the byte-cap is truncated to the cap and ends with the marker."""
    from daiv_sandbox.main import READ_MAX_OUTPUT_BYTES

    mock_session.read_file_bytes.return_value = b"a" * (READ_MAX_OUTPUT_BYTES + 100_000)
    resp = client.post(f"/session/{mock_session.session_id}/fs/read", json={"path": "/workspace/tmp/big.txt"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["encoding"] == "utf-8"
    assert "[Output truncated" in body["content"]
    assert len(body["content"].encode("utf-8")) <= READ_MAX_OUTPUT_BYTES


def test_fs_read_text_under_cap_has_no_marker(mock_session, client):
    """A small text page is returned verbatim with no truncation marker."""
    mock_session.read_file_bytes.return_value = b"hello\nworld\n"
    resp = client.post(f"/session/{mock_session.session_id}/fs/read", json={"path": "/workspace/tmp/a.txt"})
    body = resp.json()
    assert body["encoding"] == "utf-8"
    assert body["content"] == "hello\nworld"
    assert "Output truncated" not in body["content"]


def test_fs_read_binary_over_cap_is_error(mock_session, client):
    """A binary file larger than the cap returns an error rather than an unbounded base64 blob."""
    from daiv_sandbox.main import READ_MAX_OUTPUT_BYTES

    mock_session.read_file_bytes.return_value = b"\xff" * (READ_MAX_OUTPUT_BYTES + 1)
    resp = client.post(f"/session/{mock_session.session_id}/fs/read", json={"path": "/workspace/tmp/big.bin"})
    body = resp.json()
    assert body["content"] is None
    assert body["error"]["code"] == "too_large"
    assert "exceeds maximum preview size" in body["error"]["message"]


def test_fs_read_text_truncated_on_multibyte_boundary(mock_session, client):
    """A page of multi-byte chars forces the cap slice to land mid-character; decode(errors='ignore')
    must drop the partial char rather than raise, and the result stays within the cap with the marker.
    Guards the errors='ignore' branch that an all-ASCII payload never exercises."""
    from daiv_sandbox.main import READ_MAX_OUTPUT_BYTES

    # "€" is 3 bytes in UTF-8, so the byte slice almost never aligns to a character boundary.
    mock_session.read_file_bytes.return_value = ("€" * READ_MAX_OUTPUT_BYTES).encode("utf-8")
    resp = client.post(f"/session/{mock_session.session_id}/fs/read", json={"path": "/workspace/tmp/big.txt"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["encoding"] == "utf-8"
    assert "[Output truncated" in body["content"]
    assert len(body["content"].encode("utf-8")) <= READ_MAX_OUTPUT_BYTES


def test_fs_read_text_exactly_at_cap_not_truncated(mock_session, client):
    """A text page whose byte length equals the cap is returned verbatim — the boundary is strictly
    greater-than, so an off-by-one flip to >= would be caught here."""
    from daiv_sandbox.main import READ_MAX_OUTPUT_BYTES

    mock_session.read_file_bytes.return_value = b"a" * READ_MAX_OUTPUT_BYTES
    resp = client.post(f"/session/{mock_session.session_id}/fs/read", json={"path": "/workspace/tmp/exact.txt"})
    body = resp.json()
    assert body["encoding"] == "utf-8"
    assert "Output truncated" not in body["content"]
    assert len(body["content"].encode("utf-8")) == READ_MAX_OUTPUT_BYTES


def test_fs_read_binary_exactly_at_cap_is_base64(mock_session, client):
    """A binary file of exactly the cap size is still returned as base64 — only strictly larger errors."""
    from daiv_sandbox.main import READ_MAX_OUTPUT_BYTES

    raw = b"\xff" * READ_MAX_OUTPUT_BYTES
    mock_session.read_file_bytes.return_value = raw
    resp = client.post(f"/session/{mock_session.session_id}/fs/read", json={"path": "/workspace/tmp/exact.bin"})
    body = resp.json()
    assert body["encoding"] == "base64"
    assert base64.b64decode(body["content"]) == raw
    assert body["error"] is None


def test_fs_edit_string_not_found(mock_session, client):
    """edit_file raises ValueError('string_not_found') when `old` isn't present; it must map to the
    STRING_NOT_FOUND code (not MULTIPLE_OCCURRENCES, whose message starts with 'String appears')."""
    mock_session.edit_file.side_effect = ValueError("string_not_found")
    resp = client.post(
        f"/session/{mock_session.session_id}/fs/edit",
        json={"path": "/workspace/tmp/a.txt", "old": "x", "new": "y", "replace_all": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["error"]["code"] == "string_not_found"


def test_fs_edit_directory_returns_is_a_directory(mock_session, client):
    """Editing a directory path surfaces is_a_directory (inherited from read_file_bytes)."""
    mock_session.edit_file.side_effect = IsADirectoryError("/workspace/repo/src")
    resp = client.post(
        f"/session/{mock_session.session_id}/fs/edit",
        json={"path": "/workspace/repo/src", "old": "x", "new": "y", "replace_all": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["error"]["code"] == "is_a_directory"


def test_fs_edit_missing_file_returns_not_found(mock_session, client):
    """Editing a missing file surfaces not_found (normalized from the old 'file_not_found' string)."""
    mock_session.edit_file.side_effect = FileNotFoundError("/workspace/tmp/nope")
    resp = client.post(
        f"/session/{mock_session.session_id}/fs/edit",
        json={"path": "/workspace/tmp/nope", "old": "x", "new": "y", "replace_all": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["error"]["code"] == "not_found"


def test_fs_edit_multiple_occurrences(mock_session, client):
    mock_session.edit_file.side_effect = ValueError("String appears multiple times")
    resp = client.post(
        f"/session/{mock_session.session_id}/fs/edit",
        json={"path": "/workspace/tmp/a.txt", "old": "x", "new": "y", "replace_all": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["error"]["code"] == "multiple_occurrences"


def test_fs_edit_not_a_text_file(mock_session, client):
    mock_session.edit_file.side_effect = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    resp = client.post(
        f"/session/{mock_session.session_id}/fs/edit", json={"path": "/workspace/tmp/a.txt", "old": "x", "new": "y"}
    )
    assert resp.json()["error"]["code"] == "not_a_text_file"


def test_fs_edit_success_returns_count(mock_session, client):
    mock_session.edit_file.return_value = 2
    resp = client.post(
        f"/session/{mock_session.session_id}/fs/edit",
        json={"path": "/workspace/tmp/a.txt", "old": "x", "new": "y", "replace_all": True},
    )
    assert resp.json()["occurrences"] == 2


def test_fs_delete_success(mock_session, client):
    mock_session.delete_file.return_value = True
    resp = client.post(f"/session/{mock_session.session_id}/fs/delete", json={"path": "/workspace/tmp/a.txt"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["error"] is None
    assert body["removed"] is True
    mock_session.delete_file.assert_called_once()


def test_fs_delete_rejects_outside_workspace(mock_session, client):
    """A path outside /workspace is rejected (ok=False) and never reaches the destructive rm."""
    resp = client.post(f"/session/{mock_session.session_id}/fs/delete", json={"path": "/etc/evil"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is False
    mock_session.delete_file.assert_not_called()


def test_fs_delete_rejects_bare_workspace_root(mock_session, client):
    """The bare /workspace root cannot be deleted (file ops forbid the bare root)."""
    resp = client.post(f"/session/{mock_session.session_id}/fs/delete", json={"path": "/workspace"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is False
    mock_session.delete_file.assert_not_called()


def test_fs_delete_surfaces_runtime_error(mock_session, client):
    """A genuine rm failure (e.g. target is a directory) is surfaced as ok=False with an error."""
    mock_session.delete_file.side_effect = RuntimeError("rm failed: is a directory")
    resp = client.post(f"/session/{mock_session.session_id}/fs/delete", json={"path": "/workspace/tmp/d"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "exec_failed"


def test_fs_endpoints_404_when_container_missing(mock_session, client):
    mock_session.container = None
    resp = client.post(f"/session/{mock_session.session_id}/fs/read", json={"path": "/workspace/tmp/a.txt"})
    assert resp.status_code == 404, resp.text


def test_glob_to_regex_table():
    from daiv_sandbox.main import _glob_to_regex

    cases = [
        ("*.py", "a.py", True),
        ("*.py", "a.txt", False),
        ("*.py", "sub/a.py", False),  # * does not cross '/'
        ("**/*.py", "x/y/a.py", True),
        ("**/*.py", "a.py", True),
        ("a?c", "abc", True),
        ("a?c", "ac", False),
        ("[!x]file", "yfile", True),
        ("[!x]file", "xfile", False),
    ]
    for pat, s, expected in cases:
        assert bool(_glob_to_regex(pat).match(s)) is expected, (pat, s)
