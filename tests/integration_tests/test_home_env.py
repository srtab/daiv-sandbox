from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi.testclient import TestClient


def test_home_env_defaults_are_writable_uv_project(client: TestClient, sandbox_session: Callable[..., str]):
    """Commands should have a writable HOME/XDG environment by default."""
    session_id = sandbox_session(base_image="ghcr.io/astral-sh/uv:python3.14-bookworm")

    run = client.post(f"/session/{session_id}/", json={"commands": ["uv init sandbox", "uv add ruff"]})
    assert run.status_code == 200, run.text
    result = run.json()["results"][0]
    assert result["exit_code"] == 0, result["output"]


def test_home_env_defaults_are_writable_nodejs_project(client: TestClient, sandbox_session: Callable[..., str]):
    """Commands should have a writable HOME/XDG environment by default."""
    session_id = sandbox_session(base_image="node:latest")

    run = client.post(f"/session/{session_id}/", json={"commands": ["npm init -y", "npm install --save-dev prettier"]})
    assert run.status_code == 200, run.text
    result = run.json()["results"][0]
    assert result["exit_code"] == 0, result["output"]
