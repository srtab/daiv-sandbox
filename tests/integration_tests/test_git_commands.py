from typing import TYPE_CHECKING

from .utils import make_tar_gz_with_git

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi.testclient import TestClient


def test_git_commands_dont_raise_safe_directory_exception(client: TestClient, sandbox_session: Callable[..., str]):
    """Test that git commands don't raise safe directory exception."""
    session_id = sandbox_session(base_image="alpine/git:latest", extract_patch=True)

    # test that git commands don't raise safe directory exception when the repository is in the archive
    archive = make_tar_gz_with_git()
    run = client.post(f"/session/{session_id}/", json={"archive": archive, "commands": ["git status"]})
    assert run.status_code == 200, run.text
    assert run.json()["results"][0]["exit_code"] == 0, run.json()["results"][0]["output"]
    out = run.json()["results"][0]["output"]
    assert out.startswith("On branch main\n")
    assert "nothing to commit, working tree clean\n" in out
