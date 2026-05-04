"""End-to-end coverage for the eager-sync flow.

Each test starts a real sandbox session, seeds it, then exercises the
forward-and-reverse sync paths. Requires a running Docker daemon and the
sandbox image's git sidecar.
"""

import base64
from typing import TYPE_CHECKING

import pytest

from .utils import make_tar_gz

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi.testclient import TestClient


def _archive_only_readme() -> bytes:
    return make_tar_gz({"README.md": b"hello\n"})


# Parameterised across base images so we catch image-specific quirks
# (different shells, missing utilities, alpine-vs-debian path conventions).
_BASE_IMAGES = [
    "alpine:latest",
    "debian:slim",
    "ubuntu:24.04",
    "python:3.12-slim",
    "node:20-alpine",
    "golang:1.23-alpine",
]


@pytest.fixture(params=_BASE_IMAGES, ids=lambda p: p.replace(":", "_").replace("/", "_"))
def session_with_seed(request, client: TestClient, sandbox_session: Callable[..., str]):
    sid = sandbox_session(base_image=request.param, extract_patch=True)
    seed = client.post(
        f"/session/{sid}/seed/", files={"repo_archive": ("repo.tar.gz", _archive_only_readme(), "application/gzip")}
    )
    assert seed.status_code == 204, seed.text
    return sid


def test_write_then_bash_reads_synced_file(client: TestClient, session_with_seed: str):
    """A put followed by bash sees the synced content."""
    client.post(
        f"/session/{session_with_seed}/files/",
        json={
            "mutations": [
                {
                    "path": "/repo/from_daiv.txt",
                    "content": base64.b64encode(b"daiv wrote this\n").decode(),
                    "mode": 0o644,
                }
            ]
        },
    )
    resp = client.post(f"/session/{session_with_seed}/", json={"commands": ["cat /repo/from_daiv.txt"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"][0]["output"].strip() == "daiv wrote this"
    # No bash-induced workspace changes, so patch is None.
    assert body["patch"] is None


def test_bash_writes_visible_in_next_diff(client: TestClient, session_with_seed: str):
    """bash-created file shows up in the patch."""
    resp = client.post(f"/session/{session_with_seed}/", json={"commands": ["echo hi > /repo/created_by_bash.txt"]})
    body = resp.json()
    assert body["patch"] is not None
    decoded = base64.b64decode(body["patch"]).decode()
    assert "created_by_bash.txt" in decoded


def test_bash_delete_visible_in_next_diff(client: TestClient, session_with_seed: str):
    client.post(
        f"/session/{session_with_seed}/files/",
        json={
            "mutations": [{"path": "/repo/to_delete.txt", "content": base64.b64encode(b"x").decode(), "mode": 0o644}]
        },
    )
    resp = client.post(f"/session/{session_with_seed}/", json={"commands": ["rm /repo/to_delete.txt"]})
    decoded = base64.b64decode(resp.json()["patch"]).decode()
    assert "to_delete.txt" in decoded
    assert "deleted file" in decoded


def test_executable_mode_round_trips(client: TestClient, session_with_seed: str):
    """A 0o755 put is executable in the sandbox."""
    client.post(
        f"/session/{session_with_seed}/files/",
        json={
            "mutations": [
                {
                    "path": "/repo/script.sh",
                    "content": base64.b64encode(b"#!/bin/sh\necho hello\n").decode(),
                    "mode": 0o755,
                }
            ]
        },
    )
    resp = client.post(f"/session/{session_with_seed}/", json={"commands": ["/repo/script.sh"]})
    body = resp.json()
    assert body["results"][0]["exit_code"] == 0
    assert "hello" in body["results"][0]["output"]


def test_30_alternating_writes_and_bash_stay_in_sync(client: TestClient, session_with_seed: str):
    """Long alternating sequence: final state matches between client and sandbox."""
    expected = {}
    for i in range(15):
        path = f"/repo/file_{i}.txt"
        content = f"version-{i}\n".encode()
        client.post(
            f"/session/{session_with_seed}/files/",
            json={"mutations": [{"path": path, "content": base64.b64encode(content).decode(), "mode": 0o644}]},
        )
        expected[path] = content
        client.post(f"/session/{session_with_seed}/", json={"commands": [f"echo bash-{i} >> /repo/sentinel.txt"]})

    for path, expected_content in expected.items():
        resp = client.post(f"/session/{session_with_seed}/", json={"commands": [f"cat {path}"]})
        assert resp.json()["results"][0]["output"].encode() == expected_content
