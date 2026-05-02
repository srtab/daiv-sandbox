from typing import TYPE_CHECKING

from .utils import make_tar_gz

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi.testclient import TestClient


def test_extract_patch(client: TestClient, sandbox_session: Callable[..., str]):
    """Test that extract patch is not None when changes are made."""
    session_id = sandbox_session(base_image="alpine:latest", extract_patch=True)

    # Seed the workspace via the new endpoint.
    seed = client.post(
        f"/session/{session_id}/seed/", json={"repo_archive": make_tar_gz({"a.txt": b"old\n", "b.txt": b"old2\n"})}
    )
    assert seed.status_code == 204, seed.text

    # Bash-induced change is reflected in the per-turn diff.
    run = client.post(f"/session/{session_id}/", json={"commands": ["echo new > a.txt"]})
    assert run.status_code == 200, run.text
    assert run.json()["patch"] is not None

    # No changes → patch is None.
    run = client.post(f"/session/{session_id}/", json={"commands": ["ls -la"]})
    assert run.status_code == 200, run.text
    assert run.json()["patch"] is None
