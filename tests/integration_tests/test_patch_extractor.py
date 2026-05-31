"""End-to-end coverage for the patch-extractor turn diff.

Each test starts a real sandbox session, seeds it, then verifies that changes made
inside the sandbox — via bash or the fs/* endpoints — surface correctly in the
per-run patch. The sandbox is the single source of truth: there is no client-side
mirror, so a repo edit is captured by the next run's HEAD~1..HEAD diff. Requires a
running Docker daemon and the sandbox image's git sidecar.
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
    "debian:bookworm-slim",
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


def test_fs_write_then_bash_reads_and_surfaces_in_diff(client: TestClient, session_with_seed: str):
    """An fs/write under /workspace/repo is visible to bash and shows up in the next patch.

    fs/* never advances the meta HEAD itself; the change lives on the shared repo volume and is
    captured by the following run's HEAD~1..HEAD diff.
    """
    w = client.post(
        f"/session/{session_with_seed}/fs/write",
        json={
            "path": "/workspace/repo/from_daiv.txt",
            "content": base64.b64encode(b"daiv wrote this\n").decode(),
            "mode": 0o644,
        },
    )
    assert w.status_code == 200 and w.json()["ok"] is True, w.text

    resp = client.post(f"/session/{session_with_seed}/", json={"commands": ["cat /workspace/repo/from_daiv.txt"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"][0]["output"].strip() == "daiv wrote this"
    assert body["patch"] is not None
    assert "from_daiv.txt" in base64.b64decode(body["patch"]).decode()


def test_bash_writes_visible_in_next_diff(client: TestClient, session_with_seed: str):
    """bash-created file shows up in the patch."""
    resp = client.post(
        f"/session/{session_with_seed}/", json={"commands": ["echo hi > /workspace/repo/created_by_bash.txt"]}
    )
    body = resp.json()
    assert body["patch"] is not None
    decoded = base64.b64decode(body["patch"]).decode()
    assert "created_by_bash.txt" in decoded


def test_bash_delete_visible_in_next_diff(client: TestClient, session_with_seed: str):
    client.post(f"/session/{session_with_seed}/", json={"commands": ["echo x > /workspace/repo/to_delete.txt"]})
    resp = client.post(f"/session/{session_with_seed}/", json={"commands": ["rm /workspace/repo/to_delete.txt"]})
    decoded = base64.b64decode(resp.json()["patch"]).decode()
    assert "to_delete.txt" in decoded
    assert "deleted file" in decoded


def test_executable_mode_round_trips(client: TestClient, session_with_seed: str):
    """A 0o755 fs/write is executable in the sandbox."""
    w = client.post(
        f"/session/{session_with_seed}/fs/write",
        json={
            "path": "/workspace/repo/script.sh",
            "content": base64.b64encode(b"#!/bin/sh\necho hello\n").decode(),
            "mode": 0o755,
        },
    )
    assert w.status_code == 200 and w.json()["ok"] is True, w.text
    resp = client.post(f"/session/{session_with_seed}/", json={"commands": ["/workspace/repo/script.sh"]})
    body = resp.json()
    assert body["results"][0]["exit_code"] == 0
    assert "hello" in body["results"][0]["output"]


def test_readonly_run_yields_no_patch(client: TestClient, session_with_seed: str):
    """A run that changes nothing produces no patch — the extractor must not fabricate a turn.

    The single-source-of-truth flip removed the eager HEAD-advance, so this pins down that an
    empty turn diff (HEAD~1..HEAD) maps to `patch is None` rather than an empty or spurious patch.
    """
    resp = client.post(f"/session/{session_with_seed}/", json={"commands": ["cat README.md"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"][0]["output"].strip() == "hello"
    assert body["patch"] is None


def test_alternating_writes_and_bash_stay_consistent(client: TestClient, session_with_seed: str):
    """Interleaved fs/write + bash turns accumulate on the shared volume without desyncing.

    A trimmed multi-turn loop: each file reads back its last-written content after several turns,
    guarding against meta-repo / volume drift once more than one or two turns have elapsed.
    """
    expected: dict[str, bytes] = {}
    for i in range(4):
        path = f"/workspace/repo/file_{i}.txt"
        content = f"version-{i}\n".encode()
        w = client.post(
            f"/session/{session_with_seed}/fs/write",
            json={"path": path, "content": base64.b64encode(content).decode(), "mode": 0o644},
        )
        assert w.status_code == 200 and w.json()["ok"] is True, w.text
        expected[path] = content
        run = client.post(
            f"/session/{session_with_seed}/", json={"commands": [f"echo bash-{i} >> /workspace/repo/sentinel.txt"]}
        )
        assert run.status_code == 200, run.text

    for path, want in expected.items():
        run = client.post(f"/session/{session_with_seed}/", json={"commands": [f"cat {path}"]})
        assert run.status_code == 200, run.text
        assert run.json()["results"][0]["output"].encode() == want
