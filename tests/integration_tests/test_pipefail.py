from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi.testclient import TestClient


def test_pipeline_exit_code_reflects_first_failing_command(client: "TestClient", sandbox_session: "Callable[..., str]"):
    """A pipeline where an earlier stage fails should return a non-zero exit code."""
    session_id = sandbox_session(base_image="alpine:latest")

    # `false | true`: without pipefail the exit code would be 0 (from `true`);
    # with pipefail it must be 1 (from `false`).
    run = client.post(f"/session/{session_id}/", json={"commands": ["false | true"]})
    assert run.status_code == 200, run.text
    assert run.json()["results"][0]["exit_code"] != 0


def test_pipeline_exit_code_zero_when_all_succeed(client: "TestClient", sandbox_session: "Callable[..., str]"):
    """A pipeline where all stages succeed should still return exit code 0."""
    session_id = sandbox_session(base_image="alpine:latest")

    run = client.post(f"/session/{session_id}/", json={"commands": ["true | true"]})
    assert run.status_code == 200, run.text
    assert run.json()["results"][0]["exit_code"] == 0
