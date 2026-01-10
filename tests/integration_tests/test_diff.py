import base64
import io
import tarfile

import pytest
from fastapi.testclient import TestClient

from daiv_sandbox.config import settings
from daiv_sandbox.main import app


def make_tar_gz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(content)
            tf.addfile(ti, io.BytesIO(content))
    return buf.getvalue()


@pytest.fixture
def client():
    return TestClient(app, headers={"X-API-Key": settings.API_KEY.get_secret_value()}, root_path=settings.API_V1_STR)


@pytest.fixture
def archive():
    return make_tar_gz({"a.txt": b"old\n", "b.txt": b"old2\n"})


def test_extract_patch(client, archive):
    resp = client.post("/session/", json={"base_image": "alpine:3.20", "extract_patch": True})
    assert resp.status_code == 200, resp.text
    session_id = resp.json()["session_id"]

    # test that extract patch is not None when changes are made
    run = client.post(
        f"/session/{session_id}/",
        json={"archive": base64.b64encode(archive).decode(), "commands": ["echo new > a.txt"]},
    )
    assert run.status_code == 200, run.text
    assert run.json()["patch"] is not None

    # test that extract patch is None when the commands do not make any changes
    run = client.post(
        f"/session/{session_id}/", json={"archive": base64.b64encode(archive).decode(), "commands": ["ls -la"]}
    )
    assert run.status_code == 200, run.text
    assert run.json()["patch"] is None

    client.delete(f"/session/{session_id}/")


def test_network_disabled(client, archive):
    resp = client.post("/session/", json={"base_image": "alpine:3.20", "network_enabled": False})
    assert resp.status_code == 200, resp.text
    session_id = resp.json()["session_id"]

    # test that the command fails when network is disabled and the command requires network
    run = client.post(
        f"/session/{session_id}/",
        json={"archive": base64.b64encode(archive).decode(), "commands": ["ping -c 1 google.com"]},
    )
    assert run.status_code == 200, run.text
    results = run.json()["results"]
    assert results[0]["exit_code"] == 1
    assert "ping: bad address 'google.com'" in results[0]["output"]

    client.delete(f"/session/{session_id}/")
