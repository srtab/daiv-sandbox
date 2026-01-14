from typing import TYPE_CHECKING

from .utils import make_tar_gz

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi.testclient import TestClient


def test_extract_patch(client: TestClient, sandbox_session: Callable[..., str]):
    """Test that extract patch is not None when changes are made."""
    session_id = sandbox_session(base_image="alpine:latest", extract_patch=True)

    # test that extract patch is not None when changes are made
    archive = make_tar_gz({"a.txt": b"old\n", "b.txt": b"old2\n"})
    run = client.post(f"/session/{session_id}/", json={"archive": archive, "commands": ["echo new > a.txt"]})
    assert run.status_code == 200, run.text
    assert run.json()["patch"] is not None

    # test that extract patch is None when the commands do not make any changes
    run = client.post(f"/session/{session_id}/", json={"archive": archive, "commands": ["ls -la"]})
    assert run.status_code == 200, run.text
    assert run.json()["patch"] is None
