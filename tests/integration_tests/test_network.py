from typing import TYPE_CHECKING

from .utils import make_tar_gz

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi.testclient import TestClient


def test_network_disabled(client: TestClient, sandbox_session: Callable[..., str]):
    """Test that the command fails when network is disabled and the command requires network."""
    session_id = sandbox_session(base_image="alpine:latest", network_enabled=False)

    # test that the command fails when network is disabled and the command requires network
    archive = make_tar_gz({"a.txt": b"old\n", "b.txt": b"old2\n"})
    run = client.post(f"/session/{session_id}/", json={"archive": archive, "commands": ["ping -c 1 google.com"]})
    assert run.status_code == 200, run.text
    results = run.json()["results"]
    assert results[0]["exit_code"] == 1
    assert "ping: bad address 'google.com'" in results[0]["output"]
